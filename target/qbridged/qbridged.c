/*
 * qbridged - QNX resource manager that serves live system state as JSON.
 *
 * Mounts read-only files under /dev/qbridge (QNX-specific: resmgr_attach,
 * iofunc layer, procfs devctls, ThreadCtlExt):
 *
 *   /dev/qbridge/version   daemon name, version, pid
 *   /dev/qbridge/procs     processes: pid, parent, path, threads, uid, times
 *   /dev/qbridge/threads   every thread: state, priority, policy, name, cpu,
 *                          how long it has been blocked and on what
 *   /dev/qbridge/blocked   the blocked-on graph: SEND/REPLY edges with the
 *                          server pid, channel and connection ids
 *   /dev/qbridge/crashes   core files written by dumper
 *   /dev/qbridge/events    process deaths and new core dumps, as JSON lines.
 *                          A read blocks until there is something new.
 *
 * Each open() of the first five takes one consistent snapshot; reads return
 * slices of it.
 *
 * Events are pushed, not polled: the process manager (procmgr_event_notify_add)
 * and dumper (DCMD_DUMPER_NOTIFYEVENT) send pulses to qbridged's channel, and
 * qbridged answers held read()s on /dev/qbridge/events when one arrives.
 *
 * Usage: qbridged [-f] [-d core_dir] [-m mount_prefix]
 *   -f  stay in the foreground (default: become a daemon)
 */

#include <dirent.h>
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include <devctl.h>
#include <sys/dcmd_dumper.h>
#include <sys/debug.h>
#include <sys/neutrino.h>
#include <sys/procfs.h>
#include <sys/procmgr.h>
#include <sys/states.h>

struct qb_attr;
struct qb_ocb;
#define IOFUNC_ATTR_T struct qb_attr
#define IOFUNC_OCB_T  struct qb_ocb
#include <sys/iofunc.h>
#include <sys/dispatch.h>

#define QBRIDGED_VERSION "0.1.0"

#include "qb_sbuf.h"
#include "qb_deadlines.h"

/* ------------------------------------------------------------------------ */
/* Reading system state through procfs                                        */

static const char *state_name(unsigned state)
{
    static char other[16];
    static const char *names[] = {
        "DEAD", "RUNNING", "READY", "STOPPED", "SEND", "RECEIVE", "REPLY",
        "MQ_SEND", "MQ_RECEIVE", "WAITPAGE", "SIGSUSPEND", "SIGWAITINFO",
        "NANOSLEEP", "MUTEX", "CONDVAR", "JOIN", "INTR", "SEM", "WAITCTX",
        "RWLOCK_READ", "RWLOCK_WRITE", "BARRIER", "PIPE",
    };
    if (state < sizeof(names) / sizeof(names[0])) {
        return names[state];
    }
    /* Single-threaded dispatch loop, so one static buffer is safe. */
    snprintf(other, sizeof(other), "STATE_%u", state);
    return other;
}

static const char *policy_name(unsigned policy, char *buf, size_t len)
{
    switch (policy) {
    case SCHED_FIFO:     return "FIFO";
    case SCHED_RR:       return "RR";
    case SCHED_OTHER:    return "OTHER";
    case SCHED_SPORADIC: return "SPORADIC";
    default:
        /* Kernel interrupt threads report a policy outside the POSIX set. */
        snprintf(buf, len, "POLICY_%u", policy);
        return buf;
    }
}

static void read_exefile(pid_t pid, char *out, size_t outlen)
{
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/exefile", pid);
    out[0] = '\0';
    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        return;
    }
    ssize_t n = read(fd, out, outlen - 1);
    close(fd);
    if (n <= 0) {
        out[0] = '\0';
        return;
    }
    out[n] = '\0';
    /* The file ends with a NUL and/or whitespace. */
    while (n > 0 && (out[n - 1] == '\0' || out[n - 1] == ' ' || out[n - 1] == '\n')) {
        out[--n] = '\0';
    }
}

static void thread_name(pid_t pid, int tid, char *out, size_t outlen)
{
    union {
        struct _thread_name tn;
        char raw[sizeof(struct _thread_name) + _NTO_THREAD_NAME_MAX + 1];
    } u;
    memset(&u, 0, sizeof(u));
    u.tn.new_name_len = -1; /* read the name, do not change it */
    u.tn.name_buf_len = _NTO_THREAD_NAME_MAX;
    out[0] = '\0';
    if (ThreadCtlExt(pid, tid, _NTO_TCTL_NAME, &u.tn) == 0) {
        snprintf(out, outlen, "%s", u.tn.name_buf);
    }
}

static void cache_put(pid_t pid, const char *path);

typedef void (*thread_cb)(sbuf_t *b, int *first, const debug_process_t *p,
                          const char *path, const debug_thread_t *t);

/* Calls cb for every thread of every process. Returns the process count. */
static int walk_threads(sbuf_t *b, int *first, thread_cb cb,
                        void (*proc_cb)(sbuf_t *, int *, const debug_process_t *, const char *))
{
    DIR *dir = opendir("/proc");
    if (dir == NULL) {
        return 0;
    }
    int count = 0;
    struct dirent *de;
    while ((de = readdir(dir)) != NULL) {
        char *end;
        long pid = strtol(de->d_name, &end, 10);
        if (*end != '\0' || pid <= 0) {
            continue;
        }
        char ctl[64];
        snprintf(ctl, sizeof(ctl), "/proc/%ld/ctl", pid);
        int fd = open(ctl, O_RDONLY);
        if (fd < 0) {
            continue; /* process went away, or no permission */
        }
        debug_process_t info;
        memset(&info, 0, sizeof(info));
        if (devctl(fd, DCMD_PROC_INFO, &info, sizeof(info), NULL) != EOK) {
            close(fd);
            continue;
        }
        char path[PATH_MAX];
        read_exefile((pid_t)pid, path, sizeof(path));
        cache_put((pid_t)pid, path);
        count++;
        if (proc_cb) {
            proc_cb(b, first, &info, path);
        }
        if (cb) {
            /* TIDSTATUS returns the first thread whose tid >= the one asked for. */
            for (int tid = 1;; ) {
                debug_thread_t st;
                memset(&st, 0, sizeof(st));
                st.tid = tid;
                if (devctl(fd, DCMD_PROC_TIDSTATUS, &st, sizeof(st), NULL) != EOK) {
                    break;
                }
                if ((int)st.tid < tid) {
                    break;
                }
                cb(b, first, &info, path, &st);
                tid = (int)st.tid + 1;
            }
        }
        close(fd);
    }
    closedir(dir);
    return count;
}

static const char *basename_of(const char *path)
{
    const char *s = strrchr(path, '/');
    return s ? s + 1 : path;
}

/* ---- /dev/qbridge/procs ---- */

static void emit_proc(sbuf_t *b, int *first, const debug_process_t *p, const char *path)
{
    sb_printf(b, "%s\n    {\"pid\": %d, \"parent\": %d, \"name\": ", *first ? "" : ",",
              p->pid, p->parent);
    sb_json_str(b, basename_of(path));
    sb_printf(b, ", \"path\": ");
    sb_json_str(b, path);
    sb_printf(b, ", \"threads\": %u, \"uid\": %d, \"priority\": %u, \"start_ns\": %llu, "
                 "\"utime_ns\": %llu, \"stime_ns\": %llu, \"private_mem\": %llu}",
              p->num_threads, (int)p->uid, p->priority,
              (unsigned long long)p->start_time, (unsigned long long)p->utime,
              (unsigned long long)p->stime, (unsigned long long)p->private_mem);
    *first = 0;
}

/* ---- /dev/qbridge/threads ---- */

static void emit_blocked(sbuf_t *b, const debug_thread_t *t)
{
    switch (t->state) {
    case STATE_SEND:
    case STATE_REPLY:
        sb_printf(b, ", \"blocked_on\": {\"pid\": %d, \"chid\": %d, \"coid\": %d, \"scoid\": %d, \"nd\": %u}",
                  t->blocked.connect.pid, t->blocked.connect.chid, t->blocked.connect.coid,
                  t->blocked.connect.scoid, t->blocked.connect.nd);
        break;
    case STATE_RECEIVE:
        sb_printf(b, ", \"blocked_on\": {\"chid\": %d}", t->blocked.channel.chid);
        break;
    case STATE_MUTEX:
    case STATE_CONDVAR:
    case STATE_SEM:
    case STATE_RWLOCK_READ:
    case STATE_RWLOCK_WRITE:
    case STATE_BARRIER:
        sb_printf(b, ", \"blocked_on\": {\"sync\": \"0x%llx\", \"id\": %lld}",
                  (unsigned long long)t->blocked.sync.sync, (long long)t->blocked.sync.id);
        break;
    case STATE_JOIN:
        sb_printf(b, ", \"blocked_on\": {\"tid\": %d}", (int)t->blocked.join.tid);
        break;
    case STATE_WAITPAGE:
        sb_printf(b, ", \"blocked_on\": {\"pid\": %d, \"vaddr\": \"0x%llx\"}",
                  t->blocked.waitpage.pid, (unsigned long long)t->blocked.waitpage.vaddr);
        break;
    default:
        break;
    }
}

static void emit_thread(sbuf_t *b, int *first, const debug_process_t *p, const char *path,
                        const debug_thread_t *t)
{
    char tname[_NTO_THREAD_NAME_MAX + 1];
    char pbuf[24];
    thread_name(p->pid, (int)t->tid, tname, sizeof(tname));
    sb_printf(b, "%s\n    {\"pid\": %d, \"tid\": %d, \"name\": ", *first ? "" : ",",
              p->pid, (int)t->tid);
    sb_json_str(b, basename_of(path));
    sb_printf(b, ", \"path\": ");
    sb_json_str(b, path);
    sb_printf(b, ", \"thread_name\": ");
    sb_json_str(b, tname);
    sb_printf(b, ", \"state\": \"%s\", \"priority\": %u, \"real_priority\": %u, "
                 "\"policy\": \"%s\", \"last_cpu\": %u, \"blocked_ms\": %llu, \"cpu_ns\": %llu",
              state_name(t->state), t->priority, t->real_priority,
              policy_name(t->policy, pbuf, sizeof(pbuf)),
              t->last_cpu, (unsigned long long)(t->nsec_since_block / 1000000ull),
              (unsigned long long)t->sutime);
    emit_blocked(b, t);
    sb_printf(b, "}");
    *first = 0;
}

/* ---- /dev/qbridge/blocked ---- */

static void emit_edge(sbuf_t *b, int *first, const debug_process_t *p, const char *path,
                      const debug_thread_t *t)
{
    if (t->state != STATE_SEND && t->state != STATE_REPLY) {
        return;
    }
    /* Leave out the edges caused by taking this snapshot: our own devctls to
     * procnto, and the reader that is waiting for us to answer this read. */
    if (p->pid == getpid() || t->blocked.connect.pid == getpid()) {
        return;
    }
    sb_printf(b, "%s\n    {\"from_pid\": %d, \"from_tid\": %d, \"from_name\": ", *first ? "" : ",",
              p->pid, (int)t->tid);
    sb_json_str(b, basename_of(path));
    sb_printf(b, ", \"from_priority\": %u, \"state\": \"%s\", \"to_pid\": %d, \"chid\": %d, "
                 "\"blocked_ms\": %llu}",
              t->priority, state_name(t->state), t->blocked.connect.pid,
              t->blocked.connect.chid,
              (unsigned long long)(t->nsec_since_block / 1000000ull));
    *first = 0;
}

/* ---- /dev/qbridge/crashes ---- */

static const char *core_dir = "/data/var/dumper";

static void emit_crashes(sbuf_t *b)
{
    sb_printf(b, "{\"core_dir\": ");
    sb_json_str(b, core_dir);
    sb_printf(b, ", \"cores\": [");
    DIR *dir = opendir(core_dir);
    int first = 1;
    if (dir != NULL) {
        struct dirent *de;
        while ((de = readdir(dir)) != NULL) {
            if (strstr(de->d_name, ".core") == NULL) {
                continue;
            }
            char full[PATH_MAX];
            struct stat st;
            snprintf(full, sizeof(full), "%s/%s", core_dir, de->d_name);
            if (stat(full, &st) != 0) {
                continue;
            }
            sb_printf(b, "%s\n    {\"path\": ", first ? "" : ",");
            sb_json_str(b, full);
            sb_printf(b, ", \"bytes\": %lld, \"time\": %lld}",
                      (long long)st.st_size, (long long)st.st_mtime);
            first = 0;
        }
        closedir(dir);
    }
    sb_printf(b, "\n  ]}\n");
}

/* ------------------------------------------------------------------------ */
/* pid -> program path cache                                                 */
/*                                                                          */
/* A death pulse carries only the pid, and by then /proc/<pid> is gone, so  */
/* names are remembered while processes are alive: at startup, on every     */
/* PROCESS_CREATE pulse, and on every snapshot walk.                        */

#define NAME_CACHE_SIZE 1024

static struct {
    pid_t pid;
    char  path[PATH_MAX];
} name_cache[NAME_CACHE_SIZE];

static void cache_put(pid_t pid, const char *path)
{
    /* A process that has already died reads back as "(Zombie)"; keep the
     * name we had instead. */
    if (pid <= 0 || path[0] == '\0' || path[0] == '(') {
        return;
    }
    unsigned slot = (unsigned)pid % NAME_CACHE_SIZE;
    name_cache[slot].pid = pid;
    snprintf(name_cache[slot].path, sizeof(name_cache[slot].path), "%s", path);
}

static const char *cache_get(pid_t pid)
{
    unsigned slot = (unsigned)pid % NAME_CACHE_SIZE;
    return name_cache[slot].pid == pid ? name_cache[slot].path : "";
}

static void cache_proc(sbuf_t *b, int *first, const debug_process_t *p, const char *path)
{
    (void)b;
    (void)first;
    cache_put(p->pid, path);
}

/* ------------------------------------------------------------------------ */
/* Event ring                                                                 */

#define EVENT_RING 256

enum qb_event_type { EV_DEATH, EV_CORE };

struct qb_event {
    uint64_t           seq;
    long long          time;
    enum qb_event_type type;
    pid_t              pid;
    char               path[PATH_MAX];   /* program path */
    char               core[PATH_MAX];   /* core file, for EV_CORE */
};

static struct qb_event ring[EVENT_RING];
static uint64_t next_seq = 1;            /* seq of the next event to record */

static uint64_t oldest_seq(void)
{
    return next_seq > EVENT_RING ? next_seq - EVENT_RING : 1;
}

static void format_event(sbuf_t *b, const struct qb_event *e)
{
    sb_printf(b, "{\"seq\": %llu, \"time\": %lld, \"type\": \"%s\", \"pid\": %d, \"name\": ",
              (unsigned long long)e->seq, e->time, e->type == EV_DEATH ? "death" : "core",
              e->pid);
    sb_json_str(b, basename_of(e->path));
    sb_printf(b, ", \"path\": ");
    sb_json_str(b, e->path);
    if (e->type == EV_CORE) {
        sb_printf(b, ", \"core\": ");
        sb_json_str(b, e->core);
    }
    sb_printf(b, "}\n");
}

/* ------------------------------------------------------------------------ */
/* Resource manager                                                          */

enum qb_file { QB_VERSION, QB_PROCS, QB_THREADS, QB_BLOCKED, QB_CRASHES, QB_EVENTS,
               QB_DEADLINES, QB_SAMPLES, QB_NFILES };

static const char *file_names[QB_NFILES] = {
    "version", "procs", "threads", "blocked", "crashes", "events", "deadlines", "samples",
};

struct qb_attr {
    iofunc_attr_t  hdr;
    enum qb_file   which;
};

struct qb_ocb {
    iofunc_ocb_t   hdr;
    sbuf_t         snap;   /* snapshot taken on first read */
    int            ready;
    uint64_t       cursor; /* events file: next seq this reader has not seen */
};

/* Readers of /dev/qbridge/events that are waiting for an event. Their read()
 * is held: we do not reply until an event arrives, so the client stays
 * REPLY-blocked on us - the reply itself is the notification. */
#define MAX_WAITERS 32

static struct {
    int            rcvid;
    struct qb_ocb *ocb;
    size_t         nbytes;
} waiters[MAX_WAITERS];

static void build_snapshot(enum qb_file which, sbuf_t *b)
{
    int first = 1;
    struct timespec now;
    clock_gettime(CLOCK_REALTIME, &now);
    switch (which) {
    case QB_VERSION:
        sb_printf(b, "{\"name\": \"qbridged\", \"version\": \"%s\", \"pid\": %d, \"core_dir\": ",
                  QBRIDGED_VERSION, getpid());
        sb_json_str(b, core_dir);
        sb_printf(b, "}\n");
        break;
    case QB_PROCS:
        sb_printf(b, "{\"time\": %lld, \"procs\": [", (long long)now.tv_sec);
        walk_threads(b, &first, NULL, emit_proc);
        sb_printf(b, "\n  ]}\n");
        break;
    case QB_THREADS:
        sb_printf(b, "{\"time\": %lld, \"threads\": [", (long long)now.tv_sec);
        walk_threads(b, &first, emit_thread, NULL);
        sb_printf(b, "\n  ]}\n");
        break;
    case QB_BLOCKED:
        sb_printf(b, "{\"time\": %lld, \"edges\": [", (long long)now.tv_sec);
        walk_threads(b, &first, emit_edge, NULL);
        sb_printf(b, "\n  ]}\n");
        break;
    case QB_CRASHES:
        emit_crashes(b);
        break;
    case QB_DEADLINES:
        qbd_emit_deadlines(b);
        break;
    case QB_SAMPLES:
        qbd_emit_samples(b);
        break;
    default:
        break;
    }
}

static struct qb_ocb *qb_ocb_calloc(resmgr_context_t *ctp, struct qb_attr *attr)
{
    (void)ctp;
    struct qb_ocb *ocb = calloc(1, sizeof(struct qb_ocb));
    if (ocb != NULL && attr->which == QB_EVENTS) {
        /* A new reader first gets the recent history still in the ring. */
        ocb->cursor = oldest_seq();
    }
    return ocb;
}

static void qb_ocb_free(struct qb_ocb *ocb)
{
    qbd_close(ocb);   /* a task's registration ends when it closes the file */
    for (int i = 0; i < MAX_WAITERS; i++) {
        if (waiters[i].ocb == ocb) {
            waiters[i].ocb = NULL;
            waiters[i].rcvid = 0;
        }
    }
    free(ocb->snap.data);
    free(ocb);
}

/* Format the events this reader has not seen, as JSON lines, up to max bytes.
 * Only whole lines are sent. Returns the number of bytes in b. */
static size_t pending_events(struct qb_ocb *ocb, sbuf_t *b, size_t max)
{
    if (ocb->cursor < oldest_seq()) {
        ocb->cursor = oldest_seq();   /* reader fell behind; skip lost events */
    }
    while (ocb->cursor < next_seq) {
        size_t before = b->len;
        format_event(b, &ring[ocb->cursor % EVENT_RING]);
        if (b->len > max && before > 0) {
            b->len = before;          /* does not fit; leave it for the next read */
            break;
        }
        ocb->cursor++;
    }
    return b->failed ? 0 : b->len;
}

static int events_read(resmgr_context_t *ctp, io_read_t *msg, struct qb_ocb *ocb)
{
    sbuf_t b = {0};
    size_t n = pending_events(ocb, &b, msg->i.nbytes);
    if (n > 0) {
        MsgReply(ctp->rcvid, (long)n, b.data, n);
        free(b.data);
        return _RESMGR_NOREPLY;
    }
    free(b.data);
    if (ocb->hdr.ioflag & O_NONBLOCK) {
        return EAGAIN;
    }
    for (int i = 0; i < MAX_WAITERS; i++) {
        if (waiters[i].rcvid == 0) {
            waiters[i].rcvid = ctp->rcvid;
            waiters[i].ocb = ocb;
            waiters[i].nbytes = msg->i.nbytes;
            return _RESMGR_NOREPLY;   /* hold the client until an event arrives */
        }
    }
    return EBUSY;
}

/* The client wants out (signal, timeout): release a held read. */
static int qb_unblock(resmgr_context_t *ctp, io_pulse_t *msg, struct qb_ocb *ocb)
{
    (void)ocb;
    /* The unblock pulse carries the blocked client's rcvid in its value. */
    int rcvid = msg->value.sival_int ? msg->value.sival_int : ctp->rcvid;
    for (int i = 0; i < MAX_WAITERS; i++) {
        if (waiters[i].rcvid != 0 && waiters[i].rcvid == rcvid) {
            MsgError(waiters[i].rcvid, EINTR);
            waiters[i].rcvid = 0;
            waiters[i].ocb = NULL;
            break;
        }
    }
    return _RESMGR_NOREPLY;
}

static void wake_waiters(void)
{
    for (int i = 0; i < MAX_WAITERS; i++) {
        if (waiters[i].rcvid == 0 || waiters[i].ocb == NULL) {
            continue;
        }
        sbuf_t b = {0};
        size_t n = pending_events(waiters[i].ocb, &b, waiters[i].nbytes);
        if (n > 0) {
            MsgReply(waiters[i].rcvid, (long)n, b.data, n);
            waiters[i].rcvid = 0;
            waiters[i].ocb = NULL;
        }
        free(b.data);
    }
}

static void add_event(enum qb_event_type type, pid_t pid, const char *core)
{
    struct qb_event *e = &ring[next_seq % EVENT_RING];
    struct timespec now;
    clock_gettime(CLOCK_REALTIME, &now);
    memset(e, 0, sizeof(*e));
    e->seq = next_seq++;
    e->time = (long long)now.tv_sec;
    e->type = type;
    e->pid = pid;
    snprintf(e->path, sizeof(e->path), "%s", cache_get(pid));
    if (core != NULL) {
        snprintf(e->core, sizeof(e->core), "%s", core);
    }
    wake_waiters();
}

/* ------------------------------------------------------------------------ */
/* Pulses from the process manager and dumper                                 */

static int pulse_death, pulse_create, pulse_core;
static int dumper_fd = -1;

static int on_pulse(message_context_t *ctp, int code, unsigned flags, void *handle)
{
    (void)flags;
    (void)handle;
    pid_t pid = (pid_t)ctp->msg->pulse.value.sival_int;
    if (code == pulse_create) {
        char path[PATH_MAX];
        read_exefile(pid, path, sizeof(path));
        cache_put(pid, path);
    } else if (code == pulse_death) {
        /* The create pulse can arrive before a fork()ed child exec()s, so the
         * cached name may be the parent's. Try /proc once more while the
         * dying process may still be there. */
        char path[PATH_MAX];
        read_exefile(pid, path, sizeof(path));
        cache_put(pid, path);
        add_event(EV_DEATH, pid, NULL);
    } else if (code == pulse_core) {
        dump_info_t info;
        char core[PATH_MAX] = "";
        iov_t in, out;
        info.i.pid = pid;
        SETIOV(&in, &info.i, sizeof(info.i));
        SETIOV(&out, core, sizeof(core) - 1);
        if (devctlv(dumper_fd, DCMD_DUMPER_GETPATH, 1, 1, &in, &out, NULL) != EOK) {
            core[0] = '\0';
        }
        /* The process is still alive while dumper writes the core, so its
         * exefile is current here; this also fixes the name for the death
         * event that follows. */
        char path[PATH_MAX];
        read_exefile(pid, path, sizeof(path));
        cache_put(pid, path);
        add_event(EV_CORE, pid, core);
    }
    return 0;
}

/* Build a pulse sigevent aimed at our own channel. UPDATEABLE lets the
 * sender put the pid in the pulse value. */
static void make_event(struct sigevent *ev, int coid, int code)
{
    SIGEV_PULSE_INIT(ev, coid, SIGEV_PULSE_PRIO_INHERIT, code, 0);
    SIGEV_MAKE_UPDATEABLE(ev);
}

static void setup_events(dispatch_t *dpp)
{
    pulse_death = pulse_attach(dpp, MSG_FLAG_ALLOC_PULSE, 0, on_pulse, NULL);
    pulse_create = pulse_attach(dpp, MSG_FLAG_ALLOC_PULSE, 0, on_pulse, NULL);
    pulse_core = pulse_attach(dpp, MSG_FLAG_ALLOC_PULSE, 0, on_pulse, NULL);
    int coid = message_connect(dpp, MSG_FLAG_SIDE_CHANNEL);
    if (pulse_death < 0 || pulse_create < 0 || pulse_core < 0 || coid < 0) {
        fprintf(stderr, "qbridged: pulse setup failed: %s\n", strerror(errno));
        return;
    }

    /* Process manager: tell us whenever a process is created or dies. */
    struct sigevent ev_death, ev_create;
    make_event(&ev_death, coid, pulse_death);
    make_event(&ev_create, coid, pulse_create);
    if (MsgRegisterEvent(&ev_death, SYSMGR_COID) == -1 ||
        MsgRegisterEvent(&ev_create, SYSMGR_COID) == -1 ||
        procmgr_event_notify_add(PROCMGR_EVENT_PROCESS_DEATH, &ev_death) == -1 ||
        procmgr_event_notify_add(PROCMGR_EVENT_PROCESS_CREATE, &ev_create) == -1) {
        fprintf(stderr, "qbridged: procmgr events: %s\n", strerror(errno));
    }

    /* dumper: tell us when it has written a core file. */
    dumper_fd = open("/proc/dumper", O_RDONLY | O_CLOEXEC);
    if (dumper_fd < 0) {
        fprintf(stderr, "qbridged: /proc/dumper: %s (no core events)\n", strerror(errno));
        return;
    }
    struct sigevent ev_core;
    make_event(&ev_core, coid, pulse_core);
    if (MsgRegisterEvent(&ev_core, dumper_fd) == -1 ||
        devctl(dumper_fd, DCMD_DUMPER_NOTIFYEVENT, &ev_core, sizeof(ev_core), NULL) != EOK) {
        fprintf(stderr, "qbridged: dumper events: %s\n", strerror(errno));
    }
}

static int qb_read(resmgr_context_t *ctp, io_read_t *msg, struct qb_ocb *ocb)
{
    int status = iofunc_read_verify(ctp, msg, &ocb->hdr, NULL);
    if (status != EOK) {
        return status;
    }
    if ((msg->i.xtype & _IO_XTYPE_MASK) != _IO_XTYPE_NONE) {
        return ENOSYS;
    }
    if (ocb->hdr.attr->which == QB_EVENTS) {
        return events_read(ctp, msg, ocb);
    }
    if (!ocb->ready) {
        build_snapshot(ocb->hdr.attr->which, &ocb->snap);
        if (ocb->snap.failed) {
            return ENOMEM;
        }
        ocb->ready = 1;
    }

    size_t off = (size_t)ocb->hdr.offset;
    size_t left = off < ocb->snap.len ? ocb->snap.len - off : 0;
    size_t nbytes = left < msg->i.nbytes ? left : msg->i.nbytes;

    _IO_SET_READ_NBYTES(ctp, nbytes);
    if (nbytes == 0) {
        return _RESMGR_NPARTS(0);
    }
    SETIOV(ctp->iov, ocb->snap.data + off, nbytes);
    ocb->hdr.offset += nbytes;
    ocb->hdr.attr->hdr.flags |= IOFUNC_ATTR_ATIME;
    return _RESMGR_NPARTS(1);
}

/* devctl() on our files: generic commands via the iofunc layer; task
 * registration (DCMD_QBT_REGISTER) on /dev/qbridge/deadlines only. */
static int qb_devctl(resmgr_context_t *ctp, io_devctl_t *msg, struct qb_ocb *ocb)
{
    int status = iofunc_devctl_default(ctp, msg, &ocb->hdr);
    if (status != _RESMGR_DEFAULT) {
        return status;
    }
    if (ocb->hdr.attr->which != QB_DEADLINES) {
        return ENOTTY;
    }
    return qbd_devctl(ctp, msg, ocb);
}

static void usage(void)
{
    fprintf(stderr, "usage: qbridged [-f] [-d core_dir] [-m mount_prefix]\n");
    exit(EXIT_FAILURE);
}

int main(int argc, char **argv)
{
    const char *prefix = "/dev/qbridge";
    int foreground = 0;
    int c;
    while ((c = getopt(argc, argv, "fd:m:")) != -1) {
        switch (c) {
        case 'f': foreground = 1; break;
        case 'd': core_dir = optarg; break;
        case 'm': prefix = optarg; break;
        default:  usage();
        }
    }

    dispatch_t *dpp = dispatch_create();
    if (dpp == NULL) {
        perror("qbridged: dispatch_create");
        return EXIT_FAILURE;
    }

    static resmgr_connect_funcs_t connect_funcs;
    static resmgr_io_funcs_t io_funcs;
    iofunc_func_init(_RESMGR_CONNECT_NFUNCS, &connect_funcs, _RESMGR_IO_NFUNCS, &io_funcs);
    io_funcs.read = qb_read;
    io_funcs.unblock = qb_unblock;
    io_funcs.devctl = qb_devctl;

    static iofunc_funcs_t ocb_funcs = {
        .nfuncs = _IOFUNC_NFUNCS,
        .ocb_calloc = qb_ocb_calloc,
        .ocb_free = qb_ocb_free,
    };
    static iofunc_mount_t mount = { .funcs = &ocb_funcs };
    static struct qb_attr attrs[QB_NFILES];

    resmgr_attr_t rattr;
    memset(&rattr, 0, sizeof(rattr));
    rattr.nparts_max = 1;
    rattr.msg_max_size = 2048;

    for (int i = 0; i < QB_NFILES; i++) {
        char path[PATH_MAX];
        snprintf(path, sizeof(path), "%s/%s", prefix, file_names[i]);
        iofunc_attr_init(&attrs[i].hdr, S_IFREG | 0444, NULL, NULL);
        attrs[i].hdr.mount = &mount;
        attrs[i].which = (enum qb_file)i;
        if (resmgr_attach(dpp, &rattr, path, _FTYPE_ANY, 0,
                          &connect_funcs, &io_funcs, &attrs[i]) == -1) {
            fprintf(stderr, "qbridged: cannot attach %s: %s\n", path, strerror(errno));
            return EXIT_FAILURE;
        }
    }

    if (!foreground) {
        /* Detach from the terminal; keep cwd and stdio closed. */
        /* Flags 0: chdir to /, close extra fds, point stdio at /dev/null so the
         * shell (or ssh session) that started us is not held open. */
        if (procmgr_daemon(EXIT_SUCCESS, 0) == -1) {
            perror("qbridged: procmgr_daemon");
            return EXIT_FAILURE;
        }
    }

    /* After procmgr_daemon(), which closes extra file descriptors. */
    walk_threads(NULL, NULL, NULL, cache_proc);
    setup_events(dpp);
    /* Timing pulses from registered tasks are sent at qbridged's own (low)
     * priority, so delivering them never runs qbridged above the tasks. */
    struct sched_param sp;
    int policy;
    pthread_getschedparam(pthread_self(), &policy, &sp);
    if (qbd_init(dpp, sp.sched_priority) != 0) {
        fprintf(stderr, "qbridged: deadline pulses: %s\n", strerror(errno));
    }

    dispatch_context_t *ctp = dispatch_context_alloc(dpp);
    if (ctp == NULL) {
        perror("qbridged: dispatch_context_alloc");
        return EXIT_FAILURE;
    }
    for (;;) {
        if (dispatch_block(ctp) == NULL) {
            if (errno == EINTR) {
                continue;
            }
            perror("qbridged: dispatch_block");
            return EXIT_FAILURE;
        }
        dispatch_handler(ctp);
    }
}
