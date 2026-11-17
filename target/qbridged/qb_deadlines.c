/*
 * qb_deadlines.c - deadline statistics for tasks that report to qbridged.
 *
 * QNX-specific pieces:
 *   - Registration arrives as a devctl() on /dev/qbridge/deadlines
 *     (qbd_devctl). The kernel tells us who sent it: ctp->info.pid and the
 *     server connection id ctp->info.scoid.
 *   - Per-cycle timing arrives as pulses on qbridged's own channel
 *     (qbd_on_pulse, attached with pulse_attach). A pulse is accepted only if
 *     its scoid matches the connection that registered that slot, so one
 *     process cannot write into another task's statistics.
 *   - Tasks send pulses with MsgSendPulse(), which never blocks them.
 *
 * All statistics memory is a static array, allocated when qbridged starts;
 * nothing is allocated when a sample arrives. Memory is allocated only when
 * someone reads /dev/qbridge/deadlines or /dev/qbridge/samples.
 */
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/neutrino.h>

#include "qb_deadlines.h"
#include "qbridge_task.h"

#define RING        4096          /* recent cycles kept per task, for percentiles */
#define NFIELDS     5             /* backlog, latency, lock_wait, exec, response */

static const char *field_names[NFIELDS] = {
    "backlog", "latency", "lock_wait", "exec", "response",
};

struct acc {
    uint64_t sum;
    double   sumsq;
    uint32_t max;
};

struct slot {
    int        used;                    /* ever registered */
    int        active;                  /* registration file still open */
    void      *owner;                   /* OCB of the registering open() */
    int        scoid;                   /* kernel's id for that connection */
    pid_t      pid;
    int        tid;
    char       name[QBT_NAME_MAX];
    uint32_t   period_us, deadline_us;
    long long  registered, ended;       /* CLOCK_REALTIME seconds */
    int64_t    ended_ns;                /* CLOCK_MONOTONIC, for the grace period */
    uint64_t   cycles, misses, clamped, rejected;
    uint32_t   cur[NFIELDS];            /* the cycle being assembled */
    struct acc acc[NFIELDS];
    uint32_t   ring[RING][NFIELDS];     /* most recent cycles, oldest overwritten */
};

static struct slot slots[QBT_MAX_TASKS];
static int pulse_code = -1;
static int pulse_prio = 10;
static uint64_t rejected_unknown;       /* pulses for a slot this sender does not own */

static long long now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (long long)ts.tv_sec;
}

static int64_t mono_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

/* Pulses still queued when a task closes its connection are accepted for this
 * long. The close is a message sent at the task's (high) priority, so it is
 * received ahead of the task's last low-priority pulses. */
#define CLOSE_GRACE_NS 2000000000LL

static int accepts(const struct slot *sl, int scoid)
{
    if (!sl->used || scoid != sl->scoid) {
        return 0;
    }
    return sl->active || mono_ns() - sl->ended_ns < CLOSE_GRACE_NS;
}

static int qbd_on_pulse(message_context_t *ctp, int code, unsigned flags, void *handle)
{
    (void)code;
    (void)flags;
    (void)handle;
    uint32_t v = (uint32_t)ctp->msg->pulse.value.sival_int;
    unsigned s = QBT_SLOT(v), f = QBT_FIELD(v);
    struct slot *sl = &slots[s];
    if (!accepts(sl, ctp->msg->pulse.scoid)) {
        rejected_unknown++;
        return 0;
    }
    if (f < QBT_BACKLOG || f > QBT_RESPONSE) {
        sl->rejected++;
        return 0;
    }
    uint32_t us = QBT_US(v);
    if (us == QBT_US_MAX) {
        sl->clamped++;
    }
    sl->cur[f - 1] = us;
    if (f != QBT_RESPONSE) {
        return 0;
    }
    /* The response completes the cycle. */
    uint32_t *row = sl->ring[sl->cycles % RING];
    for (int i = 0; i < NFIELDS; i++) {
        row[i] = sl->cur[i];
        sl->acc[i].sum += sl->cur[i];
        sl->acc[i].sumsq += (double)sl->cur[i] * sl->cur[i];
        if (sl->cur[i] > sl->acc[i].max) {
            sl->acc[i].max = sl->cur[i];
        }
        sl->cur[i] = 0;
    }
    if (row[NFIELDS - 1] > sl->deadline_us) {
        sl->misses++;
    }
    sl->cycles++;
    return 0;
}

int qbd_init(dispatch_t *dpp, int priority)
{
    pulse_prio = priority;
    pulse_code = pulse_attach(dpp, MSG_FLAG_ALLOC_PULSE, 0, qbd_on_pulse, NULL);
    return pulse_code < 0 ? -1 : 0;
}

static struct slot *pick_slot(void)
{
    struct slot *oldest = NULL;
    for (int i = 0; i < QBT_MAX_TASKS; i++) {
        if (!slots[i].used) {
            return &slots[i];
        }
        if (!slots[i].active && (oldest == NULL || slots[i].ended < oldest->ended)) {
            oldest = &slots[i];
        }
    }
    return oldest;   /* reuse the slot of the task that ended longest ago */
}

int qbd_devctl(resmgr_context_t *ctp, io_devctl_t *msg, void *owner)
{
    if (msg->i.dcmd != DCMD_QBT_REGISTER) {
        return ENOTTY;
    }
    if (msg->i.nbytes < sizeof(union qbt_register) || pulse_code < 0) {
        return EINVAL;
    }
    union qbt_register *r = _DEVCTL_DATA(msg->i);
    struct qbt_register_in in = r->i;   /* copy before the reply overwrites it */
    if (in.protocol != QBT_PROTOCOL) {
        return EPROTONOSUPPORT;
    }
    if (in.period_us == 0 || in.deadline_us == 0) {
        return EINVAL;
    }
    for (int i = 0; i < QBT_MAX_TASKS; i++) {
        if (slots[i].active && slots[i].owner == owner) {
            return EBUSY;   /* one task per open() */
        }
    }
    struct slot *sl = pick_slot();
    if (sl == NULL) {
        return ENOSPC;
    }
    memset(sl, 0, sizeof(*sl));
    sl->used = 1;
    sl->active = 1;
    sl->owner = owner;
    sl->scoid = ctp->info.scoid;
    sl->pid = ctp->info.pid;
    sl->tid = in.tid;
    memcpy(sl->name, in.name, QBT_NAME_MAX);
    sl->name[QBT_NAME_MAX - 1] = '\0';
    for (char *p = sl->name; *p; p++) {   /* keep names safe for CSV and JSON */
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') || *p == '-' || *p == '_' || *p == '.')) {
            *p = '_';
        }
    }
    sl->period_us = in.period_us;
    sl->deadline_us = in.deadline_us;
    sl->registered = now_s();

    memset(&msg->o, 0, sizeof(msg->o));
    struct qbt_register_out *out = (struct qbt_register_out *)_DEVCTL_DATA(msg->o);
    memset(out, 0, sizeof(union qbt_register));
    out->slot = (int32_t)(sl - slots);
    out->pulse_code = pulse_code;
    out->pulse_priority = pulse_prio;
    out->protocol = QBT_PROTOCOL;
    msg->o.ret_val = EOK;
    msg->o.nbytes = sizeof(union qbt_register);
    return _RESMGR_PTR(ctp, &msg->o, sizeof(msg->o) + sizeof(union qbt_register));
}

void qbd_close(void *owner)
{
    for (int i = 0; i < QBT_MAX_TASKS; i++) {
        if (slots[i].active && slots[i].owner == owner) {
            slots[i].active = 0;
            slots[i].owner = NULL;
            slots[i].ended = now_s();
            slots[i].ended_ns = mono_ns();
        }
    }
}

static int cmp_u32(const void *a, const void *b)
{
    uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;
    return (x > y) - (x < y);
}

static void emit_field(sbuf_t *b, const struct slot *sl, int f, uint32_t *scratch)
{
    uint64_t n = sl->cycles;
    double mean = n ? (double)sl->acc[f].sum / (double)n : 0.0;
    double var = n ? sl->acc[f].sumsq / (double)n - mean * mean : 0.0;
    size_t w = n < RING ? (size_t)n : RING;
    for (size_t i = 0; i < w; i++) {
        scratch[i] = sl->ring[i][f];
    }
    uint32_t p50 = 0, p99 = 0;
    if (w > 0) {
        qsort(scratch, w, sizeof(*scratch), cmp_u32);
        p50 = scratch[(size_t)((double)(w - 1) * 0.50)];
        p99 = scratch[(size_t)((double)(w - 1) * 0.99)];
    }
    sb_printf(b, "\"%s\": {\"max_us\": %u, \"mean_us\": %.1f, \"stddev_us\": %.1f, "
                 "\"p50_us\": %u, \"p99_us\": %u}",
              field_names[f], sl->acc[f].max, mean, var > 0 ? sqrt(var) : 0.0, p50, p99);
}

void qbd_emit_deadlines(sbuf_t *b)
{
    uint32_t *scratch = malloc(RING * sizeof(*scratch));
    sb_printf(b, "{\"time\": %lld, \"pulse_code\": %d, \"window\": %d, "
                 "\"rejected_pulses\": %llu, \"tasks\": [",
              now_s(), pulse_code, RING, (unsigned long long)rejected_unknown);
    int first = 1;
    for (int i = 0; i < QBT_MAX_TASKS && scratch != NULL; i++) {
        const struct slot *sl = &slots[i];
        if (!sl->used) {
            continue;
        }
        sb_printf(b, "%s\n    {\"slot\": %d, \"name\": ", first ? "" : ",", i);
        sb_json_str(b, sl->name);
        sb_printf(b, ", \"pid\": %d, \"tid\": %d, \"active\": %s, \"registered\": %lld, "
                     "\"ended\": %lld, \"period_us\": %u, \"deadline_us\": %u, "
                     "\"cycles\": %llu, \"misses\": %llu, \"clamped\": %llu, ",
                  sl->pid, sl->tid, sl->active ? "true" : "false", sl->registered, sl->ended,
                  sl->period_us, sl->deadline_us, (unsigned long long)sl->cycles,
                  (unsigned long long)sl->misses, (unsigned long long)sl->clamped);
        for (int f = 0; f < NFIELDS; f++) {
            emit_field(b, sl, f, scratch);
            sb_printf(b, "%s", f < NFIELDS - 1 ? ", " : "");
        }
        sb_printf(b, "}");
        first = 0;
    }
    sb_printf(b, "\n  ]}\n");
    free(scratch);
    if (scratch == NULL) {
        b->failed = 1;
    }
}

void qbd_emit_samples(sbuf_t *b)
{
    sb_printf(b, "slot,task,cycle,backlog_us,latency_us,lock_wait_us,exec_us,response_us\n");
    for (int i = 0; i < QBT_MAX_TASKS; i++) {
        const struct slot *sl = &slots[i];
        if (!sl->used) {
            continue;
        }
        uint64_t first = sl->cycles > RING ? sl->cycles - RING : 0;
        for (uint64_t c = first; c < sl->cycles; c++) {
            const uint32_t *row = sl->ring[c % RING];
            sb_printf(b, "%d,%s,%llu,%u,%u,%u,%u,%u\n", i, sl->name, (unsigned long long)c,
                      row[0], row[1], row[2], row[3], row[4]);
        }
    }
}
