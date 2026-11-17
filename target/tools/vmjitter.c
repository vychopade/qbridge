/*
 * vmjitter - measure how long a CPU disappears underneath a high-priority thread.
 *
 * A thread pinned to one CPU at a high SCHED_FIFO priority reads
 * CLOCK_MONOTONIC in a tight loop for a few seconds and records every gap
 * between consecutive reads. Inside QNX only interrupt handling can preempt
 * it, which takes microseconds. Gaps of milliseconds therefore mean the CPU
 * itself stopped running the guest: on a virtual machine, the host
 * descheduled the virtual CPU. qbridge RT reports this as a limitation of
 * the timing it measures.
 *
 * With -t it measures timer wake-up lateness instead (like cyclictest): the
 * thread sleeps until each absolute period boundary with clock_nanosleep()
 * and records how late each wake-up was. Large lateness with nothing else
 * running means timers themselves fire late on this system.
 *
 * Usage: vmjitter [-t] [-c cpu] [-p priority] [-d seconds] [-i period_us]
 * Prints one JSON line.
 */
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/neutrino.h>
#include <time.h>
#include <unistd.h>

static int64_t mono_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static int timer_mode(int cpu, int prio, int seconds, int period_us)
{
    static const double edges_ms[] = {0.1, 1, 2, 5, 10, 20, 50};
    enum { NB = sizeof(edges_ms) / sizeof(edges_ms[0]) + 1 };
    long long hist[NB] = {0};
    int64_t period = (int64_t)period_us * 1000;
    int64_t start = (mono_ns() / 1000000LL + 10) * 1000000LL;
    int64_t end = start + (int64_t)seconds * 1000000000LL;
    int64_t max_late = 0, sum_late = 0;
    long long n = 0, over1ms = 0;
    for (int64_t t = start; t < end; t += period) {
        struct timespec ts = { t / 1000000000LL, t % 1000000000LL };
        while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR) {
        }
        int64_t late = mono_ns() - t;
        if (late < 0) {
            late = 0;
        }
        n++;
        sum_late += late;
        if (late > max_late) {
            max_late = late;
        }
        if (late > 1000000) {
            over1ms++;
        }
        double ms = late / 1e6;
        int b = 0;
        while (b < NB - 1 && ms >= edges_ms[b]) {
            b++;
        }
        hist[b]++;
        /* If we woke very late, skip the boundaries we slept through so each
         * stall is counted once. */
        int64_t now = mono_ns();
        while (t + period < now) {
            t += period;
        }
    }
    printf("{\"mode\": \"timer\", \"cpu\": %d, \"priority\": %d, \"seconds\": %d, "
           "\"period_us\": %d, \"wakeups\": %lld, \"max_late_us\": %.1f, "
           "\"mean_late_us\": %.1f, \"late_over_1ms\": %lld, \"late\": {"
           "\"lt_0.1ms\": %lld, \"0.1-1ms\": %lld, \"1-2ms\": %lld, \"2-5ms\": %lld, "
           "\"5-10ms\": %lld, \"10-20ms\": %lld, \"20-50ms\": %lld, \"ge_50ms\": %lld}}\n",
           cpu, prio, seconds, period_us, n, max_late / 1e3,
           n ? (double)sum_late / (double)n / 1e3 : 0.0, over1ms,
           hist[0], hist[1], hist[2], hist[3], hist[4], hist[5], hist[6], hist[7]);
    return EXIT_SUCCESS;
}

int main(int argc, char **argv)
{
    int cpu = 0, prio = 50, seconds = 5, period_us = 1000, timer = 0;
    int c;
    while ((c = getopt(argc, argv, "tc:p:d:i:")) != -1) {
        switch (c) {
        case 't': timer = 1; break;
        case 'c': cpu = atoi(optarg); break;
        case 'p': prio = atoi(optarg); break;
        case 'd': seconds = atoi(optarg); break;
        case 'i': period_us = atoi(optarg); break;
        default:
            fprintf(stderr, "usage: vmjitter [-t] [-c cpu] [-p priority] [-d seconds] [-i period_us]\n");
            return EXIT_FAILURE;
        }
    }
    if (period_us < 100 || period_us > 1000000) {
        fprintf(stderr, "vmjitter: -i must be 100..1000000 us\n");
        return EXIT_FAILURE;
    }
    if (seconds < 1 || seconds > 60 || cpu < 0 || cpu > 31 || prio < 1 || prio > 63) {
        fprintf(stderr, "vmjitter: bad arguments\n");
        return EXIT_FAILURE;
    }
    if (ThreadCtl(_NTO_TCTL_RUNMASK, (void *)(uintptr_t)(1u << cpu)) == -1) {
        perror("ThreadCtl(_NTO_TCTL_RUNMASK)");
        return EXIT_FAILURE;
    }
    struct sched_param p = { .sched_priority = prio };
    int rc = pthread_setschedparam(pthread_self(), SCHED_FIFO, &p);
    if (rc != EOK) {
        fprintf(stderr, "pthread_setschedparam: %s\n", strerror(rc));
        return EXIT_FAILURE;
    }
    if (timer) {
        return timer_mode(cpu, prio, seconds, period_us);
    }

    /* Gap histogram buckets, in ms: <0.1, <1, <2, <5, <10, <20, <50, >=50 */
    static const double edges_ms[] = {0.1, 1, 2, 5, 10, 20, 50};
    enum { NB = sizeof(edges_ms) / sizeof(edges_ms[0]) + 1 };
    long long hist[NB] = {0};
    int64_t start = mono_ns(), end = start + (int64_t)seconds * 1000000000LL;
    int64_t prev = start, max_gap = 0, lost = 0;
    long long reads = 0;
    for (;;) {
        int64_t now = mono_ns();
        int64_t gap = now - prev;
        prev = now;
        reads++;
        if (gap > max_gap) {
            max_gap = gap;
        }
        if (gap > 1000000) {
            lost += gap;
        }
        double ms = gap / 1e6;
        int b = 0;
        while (b < NB - 1 && ms >= edges_ms[b]) {
            b++;
        }
        hist[b]++;
        if (now >= end) {
            break;
        }
    }
    printf("{\"mode\": \"spin\", \"cpu\": %d, \"priority\": %d, \"seconds\": %d, \"reads\": %lld, "
           "\"max_gap_us\": %.1f, \"time_in_gaps_over_1ms_us\": %.0f, \"gaps\": {"
           "\"lt_0.1ms\": %lld, \"0.1-1ms\": %lld, \"1-2ms\": %lld, \"2-5ms\": %lld, "
           "\"5-10ms\": %lld, \"10-20ms\": %lld, \"20-50ms\": %lld, \"ge_50ms\": %lld}}\n",
           cpu, prio, seconds, reads, max_gap / 1e3, lost / 1e3,
           hist[0], hist[1], hist[2], hist[3], hist[4], hist[5], hist[6], hist[7]);
    return EXIT_SUCCESS;
}
