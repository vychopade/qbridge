/*
 * livedata2.c - step 2 of the codelab: livedata1 plus a file you can write.
 *
 *   /dev/livedata/uptime   read-only: seconds since boot and how many times
 *                          the file has been opened, freshly made on each open
 *   /dev/livedata/message  read-write: holds the last text written to it
 *
 * Build: make          Run on the target: ./livedata2 &
 * Try:   echo hello > /dev/livedata/message; cat /dev/livedata/message
 */

#include <errno.h>
#include <fcntl.h>
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

enum live_kind { LIVE_UPTIME, LIVE_MESSAGE };

/* Per-file data: the iofunc attribute structure must come first. */
struct live_attr {
    iofunc_attr_t  hdr;
    enum live_kind kind;    /* which file this is */
};

/* Per-open data (the OCB, "open control block"): the iofunc OCB must come
 * first. Each open gets its own snapshot of the text, so a reader that reads
 * in small pieces still sees one consistent version. */
struct live_ocb {
    iofunc_ocb_t hdr;
    char         text[256];
    size_t       len;
    int          wrote;     /* this open has written to the message already */
};

static unsigned opens;
static char message[256] = "nothing written yet\n";
static size_t message_len = 20;

/* Called by the iofunc layer for every open(): make the snapshot here. */
static struct live_ocb *live_ocb_calloc(resmgr_context_t *ctp, struct live_attr *attr)
{
    (void)ctp;
    struct live_ocb *ocb = calloc(1, sizeof(*ocb));
    if (ocb == NULL) {
        return NULL;
    }
    if (attr->kind == LIVE_MESSAGE) {
        memcpy(ocb->text, message, message_len);
        ocb->len = message_len;
        return ocb;
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

/* _IO_WRITE handler: the first write of an open replaces the message, later
 * writes of the same open (or any write opened with O_APPEND) add to it. As in
 * the SDP 8.0 sample handler, the data is read from the client's message just
 * after the header. */
static int live_write(resmgr_context_t *ctp, io_write_t *msg, struct live_ocb *ocb)
{
    int status = iofunc_write_verify(ctp, msg, &ocb->hdr, NULL);
    if (status != EOK) {
        return status;
    }
    if ((msg->i.xtype & _IO_XTYPE_MASK) != _IO_XTYPE_NONE) {
        return ENOSYS;
    }
    if (ocb->hdr.attr->kind != LIVE_MESSAGE) {
        return EROFS;                       /* root may open uptime for writing; refuse here */
    }
    size_t nbytes = _IO_WRITE_GET_NBYTES(msg);
    if (nbytes > (size_t)ctp->info.srcmsglen - (size_t)ctp->offset - sizeof(io_write_t)) {
        return EBADMSG;                     /* claims more data than it sent */
    }
    /* ioflag is the open() flags plus 1, so O_APPEND (>> in the shell) shows here. */
    int append = ocb->wrote || (ocb->hdr.ioflag & O_APPEND);
    size_t start = append ? message_len : 0;
    if (start + nbytes > sizeof(message)) {
        return EFBIG;
    }
    if (resmgr_msgget(ctp, message + start, nbytes, sizeof(msg->i)) == -1) {
        return errno;
    }
    message_len = start + nbytes;
    ocb->wrote = 1;
    ocb->hdr.attr->hdr.nbytes = message_len;   /* the size ls -l and stat() report */

    _IO_SET_WRITE_NBYTES(ctp, nbytes);      /* what the client's write() returns */
    if (nbytes > 0) {
        ocb->hdr.attr->hdr.flags |= IOFUNC_ATTR_MTIME | IOFUNC_ATTR_CTIME;
    }
    return _RESMGR_NPARTS(0);
}

int main(void)
{
    dispatch_t *dpp = dispatch_create();
    if (dpp == NULL) {
        perror("livedata2: dispatch_create");
        return EXIT_FAILURE;
    }

    /* Start from the default handlers, then replace the ones we need. */
    static resmgr_connect_funcs_t connect_funcs;
    static resmgr_io_funcs_t io_funcs;
    iofunc_func_init(_RESMGR_CONNECT_NFUNCS, &connect_funcs, _RESMGR_IO_NFUNCS, &io_funcs);
    io_funcs.read = live_read;
    io_funcs.write = live_write;

    /* Our own OCB allocation, attached through a mount structure. */
    static iofunc_funcs_t ocb_funcs = {
        .nfuncs = _IOFUNC_NFUNCS,
        .ocb_calloc = live_ocb_calloc,
        .ocb_free = live_ocb_free,
    };
    static iofunc_mount_t mount = { .funcs = &ocb_funcs };

    static struct live_attr uptime = { .kind = LIVE_UPTIME };
    iofunc_attr_init(&uptime.hdr, S_IFREG | 0444, NULL, NULL);
    uptime.hdr.mount = &mount;
    static struct live_attr msgfile = { .kind = LIVE_MESSAGE };
    iofunc_attr_init(&msgfile.hdr, S_IFREG | 0666, NULL, NULL);
    msgfile.hdr.mount = &mount;
    msgfile.hdr.nbytes = message_len;

    resmgr_attr_t rattr;
    memset(&rattr, 0, sizeof(rattr));
    rattr.nparts_max = 1;
    rattr.msg_max_size = 2048;

    /* Take over the path name: from now on, opens of this path come to us. */
    if (resmgr_attach(dpp, &rattr, "/dev/livedata/uptime", _FTYPE_ANY, 0,
                      &connect_funcs, &io_funcs, &uptime) == -1) {
        perror("livedata2: resmgr_attach");
        return EXIT_FAILURE;
    }
    /* A second path on the same dispatch loop: one process, two files. */
    if (resmgr_attach(dpp, &rattr, "/dev/livedata/message", _FTYPE_ANY, 0,
                      &connect_funcs, &io_funcs, &msgfile) == -1) {
        perror("livedata2: resmgr_attach");
        return EXIT_FAILURE;
    }

    dispatch_context_t *ctp = dispatch_context_alloc(dpp);
    if (ctp == NULL) {
        perror("livedata2: dispatch_context_alloc");
        return EXIT_FAILURE;
    }
    printf("livedata2: serving /dev/livedata/uptime and /dev/livedata/message\n");
    fflush(stdout);
    for (;;) {                               /* receive a message, handle it, repeat */
        if (dispatch_block(ctp) == NULL) {
            if (errno == EINTR) {
                continue;
            }
            perror("livedata2: dispatch_block");
            return EXIT_FAILURE;
        }
        dispatch_handler(ctp);
    }
}
