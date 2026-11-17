/*
 * qbridge_task.h - let a periodic real-time task report its timing to qbridged.
 *
 * QNX-specific. Two steps, both native message passing to qbridged's channel:
 *
 *   1. qbt_register(): once, before the real-time loop. Opens
 *      /dev/qbridge/deadlines and sends a devctl() (a blocking MsgSend under
 *      the hood) with the task's name, period and deadline. qbridged answers
 *      with a slot number and a pulse code.
 *
 *   2. qbt_report(): once per cycle. Sends the cycle's timing as pulses with
 *      MsgSendPulse() on the same file descriptor (on QNX a file descriptor
 *      is a connection id). Sending a pulse never waits for qbridged: the
 *      kernel queues it, so a busy or slow qbridged cannot delay the task.
 *
 * If qbridged is not running, qbt_register() fails and the task carries on
 * without reporting. Header-only; no library to link.
 */
#ifndef QBRIDGE_TASK_H
#define QBRIDGE_TASK_H

#include <devctl.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <sys/neutrino.h>

#define QBT_PATH          "/dev/qbridge/deadlines"
#define QBT_PROTOCOL      1
#define QBT_NAME_MAX      32
#define QBT_MAX_TASKS     16

/* Fields of one cycle, sent as separate pulses. QBT_RESPONSE comes last and
 * completes the cycle; fields not sent count as 0. */
enum qbt_field {
    QBT_BACKLOG   = 1,
    QBT_LATENCY   = 2,
    QBT_LOCK_WAIT = 3,
    QBT_EXEC      = 4,
    QBT_RESPONSE  = 5,
};

/* Pulse value: slot (4 bits) | field (3 bits) | microseconds (25 bits).
 * 25 bits hold up to 33.5 s; longer values are clamped and counted. */
#define QBT_US_MAX        0x1FFFFFFu
#define QBT_ENCODE(slot, field, us) \
    ((uint32_t)(((uint32_t)(slot) & 0xFu) << 28 | ((uint32_t)(field) & 0x7u) << 25 | \
                ((us) > QBT_US_MAX ? QBT_US_MAX : (uint32_t)(us))))
#define QBT_SLOT(v)   (((uint32_t)(v) >> 28) & 0xFu)
#define QBT_FIELD(v)  (((uint32_t)(v) >> 25) & 0x7u)
#define QBT_US(v)     ((uint32_t)(v) & QBT_US_MAX)

struct qbt_register_in {
    char     name[QBT_NAME_MAX];
    uint32_t period_us;
    uint32_t deadline_us;
    int32_t  tid;
    uint32_t protocol;
};

struct qbt_register_out {
    int32_t  slot;
    int32_t  pulse_code;
    int32_t  pulse_priority;
    uint32_t protocol;
};

union qbt_register {
    struct qbt_register_in  i;
    struct qbt_register_out o;
};

#define DCMD_QBT_REGISTER  __DIOTF(_DCMD_MISC, 0x71, union qbt_register)

struct qbt_task {
    int fd;          /* -1 when not registered */
    int slot;
    int code;
    int priority;
};

/* Register with qbridged. Returns 0, or an errno value (the task should then
 * run without reporting). */
static inline int qbt_register(struct qbt_task *t, const char *name,
                               uint32_t period_us, uint32_t deadline_us)
{
    t->fd = -1;
    int fd = open(QBT_PATH, O_RDONLY | O_CLOEXEC);
    if (fd == -1) {
        return errno;
    }
    union qbt_register r;
    memset(&r, 0, sizeof(r));
    strncpy(r.i.name, name, QBT_NAME_MAX - 1);
    r.i.period_us = period_us;
    r.i.deadline_us = deadline_us;
    r.i.tid = (int32_t)gettid();
    r.i.protocol = QBT_PROTOCOL;
    int rc = devctl(fd, DCMD_QBT_REGISTER, &r, sizeof(r), NULL);
    if (rc != EOK) {
        close(fd);
        return rc;
    }
    t->fd = fd;
    t->slot = r.o.slot;
    t->code = r.o.pulse_code;
    t->priority = r.o.pulse_priority;
    return EOK;
}

static inline uint32_t qbt_us(int64_t ns)
{
    return ns <= 0 ? 0u : (uint32_t)((ns + 500) / 1000 > QBT_US_MAX ? QBT_US_MAX
                                                                    : (ns + 500) / 1000);
}

/* Report one cycle (times in nanoseconds). Never blocks. Returns the number
 * of pulses the kernel refused (normally 0). */
static inline int qbt_report(const struct qbt_task *t, int64_t backlog_ns, int64_t latency_ns,
                             int64_t lock_wait_ns, int64_t exec_ns, int64_t response_ns)
{
    if (t->fd == -1) {
        return 0;
    }
    const struct { int field; int64_t ns; } f[] = {
        { QBT_BACKLOG, backlog_ns }, { QBT_LATENCY, latency_ns },
        { QBT_LOCK_WAIT, lock_wait_ns }, { QBT_EXEC, exec_ns },
        { QBT_RESPONSE, response_ns },   /* last: completes the cycle */
    };
    int failed = 0;
    for (unsigned i = 0; i < sizeof(f) / sizeof(f[0]); i++) {
        uint32_t v = QBT_ENCODE(t->slot, f[i].field, qbt_us(f[i].ns));
        if (MsgSendPulse(t->fd, t->priority, t->code, (int)v) == -1) {
            failed++;
        }
    }
    return failed;
}

static inline void qbt_close(struct qbt_task *t)
{
    if (t->fd != -1) {
        close(t->fd);
        t->fd = -1;
    }
}

#endif
