/*
 * Growable string buffer with JSON helpers, shared by qbridged's source files.
 * Used only when a client reads a /dev/qbridge file, never on the path of the
 * tasks qbridged watches.
 */
#ifndef QB_SBUF_H
#define QB_SBUF_H

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>

typedef struct {
    char  *data;
    size_t len;
    size_t cap;
    int    failed;
} sbuf_t;

static inline void sb_reserve(sbuf_t *b, size_t extra)
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

static inline void sb_printf(sbuf_t *b, const char *fmt, ...)
    __attribute__((format(printf, 2, 3)));

static inline void sb_printf(sbuf_t *b, const char *fmt, ...)
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

static inline void sb_json_str(sbuf_t *b, const char *s)
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

#endif
