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
 *
 * Each open() takes one consistent snapshot; reads return slices of it.
 *
 * Usage: qbridged [-f] [-d core_dir] [-m mount_prefix]
 *   -f  stay in the foreground (default: become a daemon)
 */

#include <dirent.h>
#include <errno.h>
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

/* ------------------------------------------------------------------------ */
/* Growable string buffer with JSON helpers                                  */

typedef struct {
    char  *data;
    size_t len;
    size_t cap;
    int    failed;
} sbuf_t;

static void sb_reserve(sbuf_t *b, size_t extra)
{
    if (b->failed || b->len + extra + 1 <= b->cap) {
        return;
    }
    size_t cap = b->cap ? b->cap : 4096;
    while (cap < b->len + extra + 1) {
        cap *= 2;
    }
    char *p = realloc(b->data, cap);
    if (p == NULL) {
        b->failed = 1;
        return;
    }
    b->data = p;
    b->cap = cap;
}

static void sb_printf(sbuf_t *b, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(NULL, 0, fmt, ap);
    va_end(ap);
    if (n < 0) {
        return;
    }
    sb_reserve(b, (size_t)n);
    if (b->failed) {
        return;
    }
    va_start(ap, fmt);
    vsnprintf(b->data + b->len, (size_t)n + 1, fmt, ap);
    va_end(ap);
    b->len += (size_t)n;
}

static void sb_json_str(sbuf_t *b, const char *s)
{
    sb_printf(b, "\"");
    for (; s && *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') {
            sb_printf(b, "\\%c", c);
        } else if (c < 0x20) {
            sb_printf(b, "\\u%04x", c);
        } else {
            sb_printf(b, "%c", c);
        }
    }
    sb_printf(b, "\"");
}

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
/* Resource manager                                                          */

enum qb_file { QB_VERSION, QB_PROCS, QB_THREADS, QB_BLOCKED, QB_CRASHES, QB_NFILES };

static const char *file_names[QB_NFILES] = {
    "version", "procs", "threads", "blocked", "crashes",
};

struct qb_attr {
    iofunc_attr_t  hdr;
    enum qb_file   which;
};

struct qb_ocb {
    iofunc_ocb_t   hdr;
    sbuf_t         snap;   /* snapshot taken on first read */
    int            ready;
};

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
    default:
        break;
    }
}

static struct qb_ocb *qb_ocb_calloc(resmgr_context_t *ctp, struct qb_attr *attr)
{
    (void)ctp;
    (void)attr;
    return calloc(1, sizeof(struct qb_ocb));
}

static void qb_ocb_free(struct qb_ocb *ocb)
{
    free(ocb->snap.data);
    free(ocb);
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
