/*
 * Deadline statistics inside qbridged: task registration (devctl) and
 * per-cycle timing pulses. See qb_deadlines.c and qbridge_task.h.
 */
#ifndef QB_DEADLINES_H
#define QB_DEADLINES_H

#include <sys/dispatch.h>
#include <sys/iofunc.h>
#include "qb_sbuf.h"

/* Attach the timing pulse code to qbridged's dispatch handle. */
int  qbd_init(dispatch_t *dpp, int pulse_priority);

/* Handle DCMD_QBT_REGISTER on /dev/qbridge/deadlines. `owner` identifies the
 * open file (the OCB); the slot is closed when that file is closed. */
int  qbd_devctl(resmgr_context_t *ctp, io_devctl_t *msg, void *owner);

/* The file `owner` was closed: the task's slot stops accepting samples. */
void qbd_close(void *owner);

/* Contents of /dev/qbridge/deadlines (JSON) and /dev/qbridge/samples (CSV). */
void qbd_emit_deadlines(sbuf_t *b);
void qbd_emit_samples(sbuf_t *b);

#endif
