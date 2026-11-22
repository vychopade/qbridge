/*
 * livedata1.c - step 1 of the codelab: a QNX resource manager that serves
 * live data as a file.
 *
 *   /dev/livedata/uptime   read-only: seconds since boot and how many times
 *                          the file has been opened, freshly made on each open
 *
 * Build: make          Run on the target: ./livedata1 &
 * Try:   cat /dev/livedata/uptime
 */

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* Tell the resource manager library which types our handlers use for the
 * per-file and per-open data (below). This must come before the includes. */
struct live_attr;
struct live_ocb;
#define IOFUNC_ATTR_T struct live_attr
#define IOFUNC_OCB_T  struct live_ocb
#include <sys/iofunc.h>
#include <sys/dispatch.h>

/* Per-file data: the iofunc attribute structure must come first. */
struct live_attr {
    iofunc_attr_t hdr;
};

/* Per-open data (the OCB, "open control block"): the iofunc OCB must come
 * first. Each open gets its own snapshot of the text, so a reader that reads
 * in small pieces still sees one consistent version. */
struct live_ocb {
    iofunc_ocb_t hdr;
    char         text[128];
    size_t       len;
};

static unsigned opens;

/* Called by the iofunc layer for every open(): make the snapshot here. */
static struct live_ocb *live_ocb_calloc(resmgr_context_t *ctp, struct live_attr *attr)
{
    (void)ctp; (void)attr;
    struct live_ocb *ocb = calloc(1, sizeof(*ocb));
    if (ocb == NULL) {
        return NULL;
    }
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    opens++;
    int n = snprintf(ocb->text, sizeof(ocb->text), "uptime_s: %lld.%03ld\nopens: %u\n",
                     (long long)now.tv_sec, now.tv_nsec / 1000000, opens);
    ocb->len = (n > 0 && (size_t)n < sizeof(ocb->text)) ? (size_t)n : 0;
    return ocb;
}

static void live_ocb_free(struct live_ocb *ocb)
{
    free(ocb);
}

/* _IO_READ handler: copy the part of the snapshot the client asked for. */
static int live_read(resmgr_context_t *ctp, io_read_t *msg, struct live_ocb *ocb)
{
    int status = iofunc_read_verify(ctp, msg, &ocb->hdr, NULL);
    if (status != EOK) {
        return status;
    }
    if ((msg->i.xtype & _IO_XTYPE_MASK) != _IO_XTYPE_NONE) {
        return ENOSYS;                      /* no pread()-style offsets */
    }
    size_t off = (size_t)ocb->hdr.offset;
    size_t left = off < ocb->len ? ocb->len - off : 0;
    size_t nbytes = left < msg->i.nbytes ? left : msg->i.nbytes;

    _IO_SET_READ_NBYTES(ctp, nbytes);       /* what the client's read() returns */
    if (nbytes == 0) {
        return _RESMGR_NPARTS(0);           /* end of file */
    }
    SETIOV(ctp->iov, ocb->text + off, nbytes);
    ocb->hdr.offset += nbytes;
    ocb->hdr.attr->hdr.flags |= IOFUNC_ATTR_ATIME;
    return _RESMGR_NPARTS(1);               /* reply with one buffer */
}

int main(void)
{
    dispatch_t *dpp = dispatch_create();
    if (dpp == NULL) {
        perror("livedata1: dispatch_create");
        return EXIT_FAILURE;
    }

    /* Start from the default handlers, then replace the ones we need. */
    static resmgr_connect_funcs_t connect_funcs;
    static resmgr_io_funcs_t io_funcs;
    iofunc_func_init(_RESMGR_CONNECT_NFUNCS, &connect_funcs, _RESMGR_IO_NFUNCS, &io_funcs);
    io_funcs.read = live_read;

    /* Our own OCB allocation, attached through a mount structure. */
    static iofunc_funcs_t ocb_funcs = {
        .nfuncs = _IOFUNC_NFUNCS,
        .ocb_calloc = live_ocb_calloc,
        .ocb_free = live_ocb_free,
    };
    static iofunc_mount_t mount = { .funcs = &ocb_funcs };

    static struct live_attr uptime;
    iofunc_attr_init(&uptime.hdr, S_IFREG | 0444, NULL, NULL);
    uptime.hdr.mount = &mount;

    resmgr_attr_t rattr;
    memset(&rattr, 0, sizeof(rattr));
    rattr.nparts_max = 1;
    rattr.msg_max_size = 2048;

    /* Take over the path name: from now on, opens of this path come to us. */
    if (resmgr_attach(dpp, &rattr, "/dev/livedata/uptime", _FTYPE_ANY, 0,
                      &connect_funcs, &io_funcs, &uptime) == -1) {
        perror("livedata1: resmgr_attach");
        return EXIT_FAILURE;
    }

    dispatch_context_t *ctp = dispatch_context_alloc(dpp);
    if (ctp == NULL) {
        perror("livedata1: dispatch_context_alloc");
        return EXIT_FAILURE;
    }
    printf("livedata1: serving /dev/livedata/uptime\n");
    fflush(stdout);
    for (;;) {                               /* receive a message, handle it, repeat */
        if (dispatch_block(ctp) == NULL) {
            if (errno == EINTR) {
                continue;
            }
            perror("livedata1: dispatch_block");
            return EXIT_FAILURE;
        }
        dispatch_handler(ctp);
    }
}
