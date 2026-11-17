/*
 * Demo app with a planted bug: priority inversion over QNX message passing.
 *
 * Three processes share CPU 0:
 *   server  priority 10  answers requests; each one takes ~20 ms of CPU
 *   client  priority 30  sends a request every 100 ms and times the answer
 *   hog     priority 20  spins from 2 s until 2 s before the end
 *                        (think: a busy logger)
 *
 * Normally QNX avoids inversion here: a server thread that receives a message
 * runs at the sender's priority (priority inheritance), so the server would
 * run at 30 and preempt the hog. But the server's channel is created with
 * _NTO_CHF_FIXED_PRIORITY, which turns that off. Once the hog starts, the
 * server (10) never gets the CPU, so the high-priority client (30) waits
 * behind a medium-priority process (20).
 *
 * Usage: inversion [seconds]   (default 20)
 */
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/neutrino.h>
#include <sys/syspage.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define SERVER_PRIO 10
#define HOG_PRIO    20
#define CLIENT_PRIO 30
#define WORK_MS     20

static uint64_t now_ms(void);

static void setup_thread(int prio, const char *name)
{
    /* Pin to CPU 0 so all three compete for one CPU. */
    if (ThreadCtl(_NTO_TCTL_RUNMASK, (void *)(uintptr_t)0x1) == -1) {
        perror("ThreadCtl(RUNMASK)");
    }
    struct sched_param p = { .sched_priority = prio };
    if (pthread_setschedparam(pthread_self(), SCHED_FIFO, &p) != 0) {
        perror("pthread_setschedparam");
    }
    pthread_setname_np(pthread_self(), name);
}

static uint64_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u;
}

static void burn_cpu_ms(unsigned ms)
{
    /* Busy work: counts CPU cycles actually spent running, so being preempted
     * makes it take longer in wall-clock time. */
    uint64_t cps = SYSPAGE_ENTRY(qtime)->cycles_per_sec;
    uint64_t need = cps / 1000u * ms;
    uint64_t done = 0;
    uint64_t last = ClockCycles();
    while (done < need) {
        uint64_t c = ClockCycles();
        uint64_t d = c - last;
        last = c;
        if (d < cps / 10000u) {   /* < 0.1 ms: we were running, count it */
            done += d;
        }
    }
}

static void run_hog(pid_t parent, int busy_seconds)
{
    setup_thread(HOG_PRIO, "hog-medium");
    sleep(2);
    /* Spin for busy_seconds, but stop early if the demo's parent goes away,
     * so a stopped demo never leaves a priority-20 spinner on CPU 0. */
    uint64_t end = now_ms() + (uint64_t)busy_seconds * 1000u;
    for (unsigned long i = 0;; i++) {
        if ((i & 0xfffff) == 0 && (now_ms() >= end || getppid() != parent)) {
            exit(EXIT_SUCCESS);
        }
    }
}

static void run_client(pid_t server, int chid, int seconds)
{
    setup_thread(CLIENT_PRIO, "client-high");
    int coid = ConnectAttach(0, server, chid, _NTO_SIDE_CHANNEL, 0);
    if (coid == -1) {
        perror("client: ConnectAttach");
        exit(EXIT_FAILURE);
    }
    uint64_t end = now_ms() + (uint64_t)seconds * 1000u;
    uint64_t worst = 0;
    int n = 0;
    while (now_ms() < end) {
        uint64_t t0 = now_ms();
        int req = n, rep = 0;
        if (MsgSend(coid, &req, sizeof(req), &rep, sizeof(rep)) == -1) {
            perror("client: MsgSend");
            break;
        }
        uint64_t took = now_ms() - t0;
        if (took > worst) {
            worst = took;
        }
        printf("request %d answered in %llu ms\n", n, (unsigned long long)took);
        fflush(stdout);
        n++;
        usleep(100 * 1000);
    }
    printf("client: %d requests, worst %llu ms (expected about %d ms)\n",
           n, (unsigned long long)worst, WORK_MS);
    fflush(stdout);
    int done = -1, rep;
    MsgSend(coid, &done, sizeof(done), &rep, sizeof(rep));   /* tell the server to stop */
    exit(EXIT_SUCCESS);
}

int main(int argc, char **argv)
{
    int seconds = argc > 1 ? atoi(argv[1]) : 20;

    /* BUG: _NTO_CHF_FIXED_PRIORITY disables priority inheritance, so the
     * server keeps running at its own low priority while serving a
     * high-priority client. */
    int chid = ChannelCreate(_NTO_CHF_FIXED_PRIORITY);
    if (chid == -1) {
        perror("ChannelCreate");
        return EXIT_FAILURE;
    }
    pid_t server = getpid();

    /* The hog runs from 2 s until 2 s before the end. */
    int busy = seconds > 6 ? seconds - 4 : 2;
    pid_t hog = fork();
    if (hog == 0) {
        run_hog(server, busy);
    }
    pid_t client = fork();
    if (client == 0) {
        run_client(server, chid, seconds);
    }

    setup_thread(SERVER_PRIO, "server-low");
    printf("server %d, client %d, hog %d (hog starts after 2 s)\n", server, client, hog);
    fflush(stdout);

    for (;;) {
        int req;
        int rcvid = MsgReceive(chid, &req, sizeof(req), NULL);
        if (rcvid == -1) {
            perror("server: MsgReceive");
            break;
        }
        if (rcvid == 0) {
            continue; /* pulse */
        }
        if (req < 0) {
            MsgReply(rcvid, EOK, &req, sizeof(req));
            break;    /* client finished its run */
        }
        burn_cpu_ms(WORK_MS);
        int rep = req;
        MsgReply(rcvid, EOK, &rep, sizeof(rep));
    }
    waitpid(client, NULL, 0);
    kill(hog, SIGKILL);
    waitpid(hog, NULL, 0);
    return EXIT_SUCCESS;
}
