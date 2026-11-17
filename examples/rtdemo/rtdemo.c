/*
 * rtdemo - a simulated real-time control task that misses its deadline.
 *
 * Educational demo for qbridge RT. Three threads, all pinned to one CPU
 * (CPU 1 by default, see -c below):
 *
 *   A  "control"   priority 30  period 10 ms, deadline 10 ms. Each cycle it
 *                               takes the shared lock briefly, then does
 *                               about 1 ms of work. It measures itself.
 *   B  "worker"    priority 20  every 100 ms, a 40 ms CPU-heavy burst.
 *   C  "logger"    priority 10  every 20 ms, takes the shared lock and holds
 *                               it for 3 ms of work.
 *
 * Planted fault: the shared lock is created with PTHREAD_PRIO_NONE, which
 * turns off QNX's default priority inheritance (the default for a mutex on
 * QNX 8.0 is PTHREAD_PRIO_INHERIT). When B's burst starts while C holds the
 * lock, B preempts C, A blocks on the lock, and A waits for B's whole burst.
 * This is priority inversion. With inheritance, C runs at A's priority as
 * soon as A waits, and A's wait is at most C's remaining 3 ms.
 *
 * The workload is fixed (same phases every run) so before/after runs are
 * comparable. Timing on a virtual machine is noisy; finite runs show the
 * effect, they do not prove a worst case.
 *
 * Usage: rtdemo [-d seconds] [-o samples.csv] [-c cpu] [-n]
 *   Prints one JSON summary line on stdout when done.
 *   If qbridged is running, A registers with it and reports every cycle by
 *   message passing (see target/qbridged/qbridge_task.h); -n turns that off.
 *
 * Why CPU 1 by default: on the reference VirtualBox target, timers on
 * virtual CPU 0 fire 10-55 ms late even when idle, while CPU 1 is within
 * about 1.4 ms (measured with target/tools/vmjitter -t). On CPU 0 that noise
 * swamps the effect being demonstrated. Check your own target with vmjitter.
 */
#include <errno.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/neutrino.h>
#include <sys/syspage.h>
#include <time.h>

#include "qbridge_task.h"   /* report each cycle to qbridged (target/qbridged) */
#include <unistd.h>

/* ---- The fault, in one place ------------------------------------------- */
/* PTHREAD_PRIO_NONE: holding the lock does not raise the holder's priority.
 * Restoring PTHREAD_PRIO_INHERIT (QNX's default) is the fix. */
#define LOCK_PROTOCOL PTHREAD_PRIO_NONE

/* ---- Fixed workload ------------------------------------------------------- */
#define A_PRIO        30
#define B_PRIO        20
#define C_PRIO        10
#define A_PERIOD_NS   10000000LL   /* 10 ms */
#define A_DEADLINE_NS 10000000LL   /* 10 ms, relative to release */
#define A_LOCKED_NS     200000LL   /* 0.2 ms of work inside the lock */
#define A_WORK_NS       800000LL   /* 0.8 ms of work outside it */
#define B_PERIOD_NS  100000000LL   /* 100 ms */
#define B_OFFSET_NS    3000000LL   /* starts 3 ms into each 100 ms window */
#define B_BURST_NS    40000000LL   /* 40 ms of CPU */
#define C_PERIOD_NS   20000000LL   /* 20 ms */
#define C_OFFSET_NS    2000000LL   /* starts 2 ms into each 20 ms window */
#define C_HOLD_NS      3000000LL   /* 3 ms of work while holding the lock */

static pthread_mutex_t shared_lock;
static int run_cpu = 1;         /* all three threads run on this CPU */
static int report_to_qbridged = 1;
static int qbt_status = -1;     /* qbt_register() result; -1 = not tried */
static int qbt_slot = -1;
static long qbt_failures;       /* pulses the kernel refused */
static int64_t t0;              /* common start time, CLOCK_MONOTONIC ns */
static int64_t t_end;           /* run ends here */

struct sample {
    int64_t release;    /* scheduled release, ns since t0 */
    int64_t backlog;    /* how late the previous cycle finished past this
                           release (0 if it finished in time) */
    int64_t latency;    /* scheduling latency: wake-up time minus the moment
                           A could first have started (release, or the end of
                           the previous cycle if that was later) */
    int64_t lock_wait;  /* time spent waiting for the shared lock */
    int64_t exec;       /* CPU time A actually used this cycle */
    int64_t response;   /* finish time minus release */
};

static struct sample *samples;  /* allocated once, before the run */
static size_t max_samples;
static size_t n_samples;

static int64_t mono_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static int64_t cpu_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static void sleep_until(int64_t abs_ns)
{
    struct timespec ts = { abs_ns / 1000000000LL, abs_ns % 1000000000LL };
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR) {
    }
}

/* Burn this much of the calling thread's own CPU time. Being preempted
 * does not count, so the work is the same however long it takes. */
static void burn(int64_t ns)
{
    int64_t start = cpu_ns();
    while (cpu_ns() - start < ns) {
    }
}

static void setup_thread(int prio, const char *name)
{
    if (ThreadCtl(_NTO_TCTL_RUNMASK, (void *)(uintptr_t)(1u << run_cpu)) == -1) {
        perror("ThreadCtl(_NTO_TCTL_RUNMASK)");
    }
    struct sched_param p = { .sched_priority = prio };
    int rc = pthread_setschedparam(pthread_self(), SCHED_FIFO, &p);
    if (rc != EOK) {
        fprintf(stderr, "pthread_setschedparam(%d): %s\n", prio, strerror(rc));
    }
    pthread_setname_np(pthread_self(), name);
}

static void *task_a(void *arg)
{
    (void)arg;
    setup_thread(A_PRIO, "A-control");
    /* Register with qbridged before the real-time loop (this one call may
     * block briefly; the per-cycle reports below never do). */
    struct qbt_task qbt = { .fd = -1 };
    if (report_to_qbridged) {
        qbt_status = qbt_register(&qbt, "A-control", A_PERIOD_NS / 1000, A_DEADLINE_NS / 1000);
        qbt_slot = qbt_status == EOK ? qbt.slot : -1;
    }
    int64_t prev_done = 0;
    for (int64_t k = 1;; k++) {
        int64_t release = t0 + k * A_PERIOD_NS;
        if (release >= t_end || n_samples >= max_samples) {
            break;
        }
        sleep_until(release);       /* returns at once if we are late */
        int64_t wake = mono_ns();
        int64_t ready = prev_done > release ? prev_done : release;
        int64_t c0 = cpu_ns();

        int64_t l0 = mono_ns();
        pthread_mutex_lock(&shared_lock);
        int64_t l1 = mono_ns();
        burn(A_LOCKED_NS);
        pthread_mutex_unlock(&shared_lock);
        burn(A_WORK_NS);

        int64_t c1 = cpu_ns();          /* read CPU time before the end stamp */
        int64_t done = mono_ns();
        struct sample *s = &samples[n_samples++];
        s->release = release - t0;
        s->backlog = ready - release;
        s->latency = wake - ready;
        prev_done = done;
        s->lock_wait = l1 - l0;
        s->exec = c1 - c0;
        s->response = done - release;
        /* After the cycle's own timestamps, so reporting is not counted in
         * this cycle's response. MsgSendPulse never blocks. */
        qbt_failures += qbt_report(&qbt, s->backlog, s->latency, s->lock_wait, s->exec,
                                   s->response);
    }
    qbt_close(&qbt);
    return NULL;
}

static void *task_b(void *arg)
{
    (void)arg;
    setup_thread(B_PRIO, "B-worker");
    for (int64_t k = 0;; k++) {
        int64_t release = t0 + B_OFFSET_NS + k * B_PERIOD_NS;
        if (release + B_BURST_NS >= t_end) {
            break;
        }
        sleep_until(release);
        burn(B_BURST_NS);
    }
    return NULL;
}

static void *task_c(void *arg)
{
    (void)arg;
    setup_thread(C_PRIO, "C-logger");
    for (int64_t k = 0;; k++) {
        int64_t release = t0 + C_OFFSET_NS + k * C_PERIOD_NS;
        if (release >= t_end) {
            break;
        }
        sleep_until(release);
        pthread_mutex_lock(&shared_lock);
        burn(C_HOLD_NS);
        pthread_mutex_unlock(&shared_lock);
    }
    return NULL;
}

struct stats {
    double max, mean, stddev, p99;
};

static int cmp_i64(const void *a, const void *b)
{
    int64_t x = *(const int64_t *)a, y = *(const int64_t *)b;
    return (x > y) - (x < y);
}

static struct stats stats_of(size_t field_offset)
{
    struct stats st = {0};
    if (n_samples == 0) {
        return st;
    }
    int64_t *v = malloc(n_samples * sizeof(*v));
    double sum = 0;
    for (size_t i = 0; i < n_samples; i++) {
        v[i] = *(int64_t *)((char *)&samples[i] + field_offset);
        sum += (double)v[i];
    }
    st.mean = sum / (double)n_samples;
    double var = 0;
    for (size_t i = 0; i < n_samples; i++) {
        double d = (double)v[i] - st.mean;
        var += d * d;
    }
    st.stddev = sqrt(var / (double)n_samples);
    qsort(v, n_samples, sizeof(*v), cmp_i64);
    st.max = (double)v[n_samples - 1];
    st.p99 = (double)v[(size_t)((double)(n_samples - 1) * 0.99)];
    free(v);
    return st;
}

static void print_stats(const char *name, struct stats st, int last)
{
    printf("\"%s\": {\"max_us\": %.0f, \"mean_us\": %.1f, \"stddev_us\": %.1f, \"p99_us\": %.0f}%s",
           name, st.max / 1e3, st.mean / 1e3, st.stddev / 1e3, st.p99 / 1e3, last ? "" : ", ");
}

int main(int argc, char **argv)
{
    int seconds = 10;
    const char *csv = "/data/qbridge/rtdemo_samples.csv";
    int c;
    while ((c = getopt(argc, argv, "d:o:c:n")) != -1) {
        switch (c) {
        case 'd': seconds = atoi(optarg); break;
        case 'o': csv = optarg; break;
        case 'c': run_cpu = atoi(optarg); break;
        case 'n': report_to_qbridged = 0; break;
        default:
            fprintf(stderr, "usage: rtdemo [-d seconds] [-o samples.csv] [-c cpu] [-n]\n");
            return EXIT_FAILURE;
        }
    }
    if (run_cpu < 0 || run_cpu >= _syspage_ptr->num_cpu) {
        fprintf(stderr, "rtdemo: -c must be 0..%d\n", _syspage_ptr->num_cpu - 1);
        return EXIT_FAILURE;
    }
    if (seconds < 1 || seconds > 600) {
        fprintf(stderr, "rtdemo: -d must be 1..600\n");
        return EXIT_FAILURE;
    }

    /* All memory for samples is allocated before the run starts. */
    max_samples = (size_t)seconds * (1000000000LL / A_PERIOD_NS) + 16;
    samples = calloc(max_samples, sizeof(*samples));
    if (samples == NULL) {
        perror("calloc");
        return EXIT_FAILURE;
    }

    pthread_mutexattr_t ma;
    pthread_mutexattr_init(&ma);
    int rc = pthread_mutexattr_setprotocol(&ma, LOCK_PROTOCOL);
    if (rc != EOK) {
        fprintf(stderr, "pthread_mutexattr_setprotocol: %s\n", strerror(rc));
        return EXIT_FAILURE;
    }
    pthread_mutex_init(&shared_lock, &ma);
    pthread_mutexattr_destroy(&ma);

    /* Start 50 ms from now, aligned to a whole millisecond. */
    t0 = (mono_ns() / 1000000LL + 50) * 1000000LL;
    t_end = t0 + (int64_t)seconds * 1000000000LL;

    pthread_t ta, tb, tc;
    pthread_create(&tc, NULL, task_c, NULL);
    pthread_create(&tb, NULL, task_b, NULL);
    pthread_create(&ta, NULL, task_a, NULL);
    pthread_join(ta, NULL);
    pthread_join(tb, NULL);
    pthread_join(tc, NULL);

    size_t misses = 0;
    for (size_t i = 0; i < n_samples; i++) {
        if (samples[i].response > A_DEADLINE_NS) {
            misses++;
        }
    }

    FILE *f = fopen(csv, "w");
    if (f != NULL) {
        fprintf(f, "release_us,backlog_us,latency_us,lock_wait_us,exec_us,response_us,missed\n");
        for (size_t i = 0; i < n_samples; i++) {
            const struct sample *s = &samples[i];
            fprintf(f, "%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%d\n", s->release / 1e3,
                    s->backlog / 1e3, s->latency / 1e3,
                    s->lock_wait / 1e3, s->exec / 1e3, s->response / 1e3,
                    s->response > A_DEADLINE_NS);
        }
        fclose(f);
    } else {
        fprintf(stderr, "rtdemo: cannot write %s: %s\n", csv, strerror(errno));
    }

    const char *protocol = LOCK_PROTOCOL == PTHREAD_PRIO_INHERIT ? "PTHREAD_PRIO_INHERIT"
                         : LOCK_PROTOCOL == PTHREAD_PRIO_NONE    ? "PTHREAD_PRIO_NONE"
                                                                 : "OTHER";
    printf("{\"task\": \"A-control\", \"cpu\": %d, \"lock_protocol\": \"%s\", "
           "\"period_us\": %lld, \"deadline_us\": %lld, \"seconds\": %d, "
           "\"cycles\": %zu, \"misses\": %zu, ",
           run_cpu, protocol, A_PERIOD_NS / 1000, A_DEADLINE_NS / 1000, seconds,
           n_samples, misses);
    print_stats("response", stats_of(offsetof(struct sample, response)), 0);
    print_stats("backlog", stats_of(offsetof(struct sample, backlog)), 0);
    print_stats("latency", stats_of(offsetof(struct sample, latency)), 0);
    print_stats("lock_wait", stats_of(offsetof(struct sample, lock_wait)), 0);
    print_stats("exec", stats_of(offsetof(struct sample, exec)), 1);
    printf(", \"samples_csv\": \"%s\", \"qbridged\": {\"status\": \"%s\", \"slot\": %d, "
           "\"refused_pulses\": %ld}}\n", csv,
           qbt_status == -1 ? "off" : qbt_status == EOK ? "registered" : strerror(qbt_status),
           qbt_slot, qbt_failures);
    return EXIT_SUCCESS;
}
