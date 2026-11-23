summary: Write a QNX 8.0 resource manager that serves live data as files
id: qnx-resmgr-live-files
categories: QNX, resource manager, message passing
environments: Web
status: Draft
authors: vychopade
feedback link: https://github.com/vychopade/qbridge/issues

# Write a QNX resource manager that serves live data as files

## Overview
Duration: 0:03

On QNX, many things that look like files are served by an ordinary program. `/dev/null`, serial ports, network sockets and file systems are all **resource managers**: processes that take over a path name and answer the `open()`, `read()` and `write()` calls made on it.

In this codelab you write one. It is a small C program that serves live data under `/dev/livedata`. Any program, including the shell, can then read it with `cat` and change it with `echo`. You need no special client library and no driver framework.

### What you'll build

- **Step 1:** `/dev/livedata/uptime`, a read-only file. Each time it is opened, it says how long the system has been up and how many times the file has been opened.
- **Step 2:** `/dev/livedata/message`, a file you can write text into and read back. The server also refuses writes to `uptime`.

### What you'll learn

- How a client's `open()`, `read()` and `write()` become **messages** to your process.
- How to take over a path with `resmgr_attach()` and serve it with a dispatch loop.
- What the **iofunc** layer gives you for free and how to replace just one handler.
- Why a per-open **OCB** (open control block) is the right place for a snapshot.

Every command and every output in this codelab was run on a QNX 8.0 x86_64 target (a `mkqnximage` VM) on Nov 22, 2026. Times and process IDs will differ on yours.

## What you need
Duration: 0:05

- **The QNX SDP 8.0** on your computer, with `qcc` (this codelab used it on Windows 11). The free QNX Everywhere licence is enough.
- **A QNX 8.0 target** that you can reach as `root` with `ssh` and `scp`, for example a VM made with QNX's `mkqnximage` tool. This codelab used a 2-CPU x86_64 VirtualBox VM. The commands below write `TARGET` for its address; replace it with yours (`mkqnximage --getip`, run in the image's folder, prints it).
- The example code: clone `https://github.com/vychopade/qbridge` and use the folder `docs/codelab/livedata`. It holds `livedata1.c` (step 1), `livedata2.c` (step 2) and a `Makefile`.

Negative
: **Windows and "Corrupted MAC on input".** With Windows' built-in OpenSSH and the QNX 8.0 `sshd`, `ssh` and `scp` can fail with this message. Add `-o MACs=hmac-sha2-256` to the command, for example `scp -o MACs=hmac-sha2-256 ...`.

## How a resource manager works
Duration: 0:05

QNX is a microkernel. File systems and drivers are not inside the kernel; they are processes. When a program calls `open("/dev/livedata/uptime")`:

1. The C library asks the **process manager**, which keeps the table of path names: "Who serves this path?"
2. The answer is a process and a channel. The library connects to it.
3. `open()`, `read()`, `write()`, `stat()` and `close()` are sent as **messages** (`MsgSend`). The server receives each one, works out the answer and **replies**. The client is blocked until the reply comes.

```
  cat                          livedata1
  read(fd, buf, 4096)  ──── _IO_READ message ────►  live_read()
        (blocked)                                    copies some text
  returns 30 bytes     ◄──────── reply ───────────   into the reply
```

You don't write the message decoding yourself. QNX's resource manager library has two layers:

- The **dispatch layer** (`dispatch_create`, `dispatch_block`, `dispatch_handler`) receives messages and calls the handler for each message type.
- The **iofunc layer** (`iofunc_func_init`, `iofunc_attr_init`, `iofunc_*_default`) provides default handlers with POSIX behaviour: permissions, `stat()`, the file offset, `close()`. You replace only the handlers you care about.

## Build the example
Duration: 0:03

On Windows, open a **Command Prompt**, set up the QNX environment, and build. On Linux, use `source ~/qnx800/qnxsdp-env.sh` instead of the first line.

```
C:\Users\you\qnx800\qnxsdp-env.bat
cd qbridge\docs\codelab\livedata
make
```

Output:

```
qcc -Vgcc_ntox86_64 -g -Wall -Wextra -o livedata1 livedata1.c
qcc -Vgcc_ntox86_64 -g -Wall -Wextra -o livedata2 livedata2.c
```

`-Vgcc_ntox86_64` selects the compiler for an x86_64 QNX target. The programs only need the standard C library, which contains the resource manager functions, so there are no extra libraries to link.

Copy both programs to the target. `/data` is writable on a `mkqnximage` target:

```
scp livedata1 livedata2 root@TARGET:/data/
```

## Step 1: run it and read the file
Duration: 0:05

Log in to the target and make the programs executable. `scp` from Windows does not keep the execute bit; without `chmod` you get `cannot execute - Permission denied`.

```
ssh root@TARGET
cd /data
chmod +x livedata1 livedata2
./livedata1 &
```

```
[1] 13320217
livedata1: serving /dev/livedata/uptime
```

Now read the file twice:

```
# cat /dev/livedata/uptime
uptime_s: 4025.837
opens: 1
# cat /dev/livedata/uptime
uptime_s: 4025.845
opens: 2
```

The numbers are made at the moment you open the file. There is no file on any disk: the text comes from `livedata1`.

```
# ls -l /dev/livedata
total 0
-r--r--r--  1 root root 0 2026-11-22 20:09 uptime
```

The permissions (`-r--r--r--`) and the size (0) come from the attribute structure the program set up with `iofunc_attr_init()`. Size 0 is honest: the text is made on demand, so the server doesn't know its length in advance. `cat` reads until a read returns 0 bytes, so it still gets everything.

What is the server doing between requests?

```
# pidin -p livedata1
     pid tid name                         prio STATE          Blocked
13320217   1 ./livedata1                   10r RECEIVE        1
```

`RECEIVE` means it is blocked in `MsgReceive()`, waiting for the next message, and uses no CPU at all. (`10r` is priority 10, round-robin scheduling.)

Positive
: Try `ls -l /dev/livedata/uptime` and then `cat` the file again: `opens` goes up by 2, not 1. On QNX, `stat()` is also a message to the server and goes through the same open path, so the server's `ocb_calloc` runs for it too: on our target each `ls -l` of the file counted as one open.

## Step 1: the code
Duration: 0:10

Open `livedata1.c`. It has four parts.

### 1. Your own types for the file and for each open

```c
struct live_attr;
struct live_ocb;
#define IOFUNC_ATTR_T struct live_attr
#define IOFUNC_OCB_T  struct live_ocb
#include <sys/iofunc.h>
#include <sys/dispatch.h>

struct live_attr {            /* one per file */
    iofunc_attr_t hdr;        /* must be first */
};

struct live_ocb {             /* one per open() */
    iofunc_ocb_t hdr;         /* must be first */
    char         text[128];
    size_t       len;
};
```

The library keeps two records. The **attribute** (`iofunc_attr_t`) describes the file: its mode, owner, size and times. The **OCB** (`iofunc_ocb_t`) describes one open of it: the access mode and the current offset. You extend both by putting the library's structure first and your fields after it. The two `#define`s, placed *before* the includes, make the library's prototypes use your types, so handlers receive `struct live_ocb *` directly.

### 2. A snapshot per open

```c
static struct live_ocb *live_ocb_calloc(resmgr_context_t *ctp, struct live_attr *attr)
{
    struct live_ocb *ocb = calloc(1, sizeof(*ocb));
    ...
    clock_gettime(CLOCK_MONOTONIC, &now);
    opens++;
    snprintf(ocb->text, sizeof(ocb->text), "uptime_s: %lld.%03ld\nopens: %u\n", ...);
    return ocb;
}
```

The iofunc layer calls this when it creates an OCB, which happens once per `open()`. Making the text here means each reader gets its own snapshot, so a program that reads 5 bytes at a time still sees one consistent version. If the text were rebuilt on every `read()`, the second piece could come from a newer version than the first.

### 3. The read handler

```c
static int live_read(resmgr_context_t *ctp, io_read_t *msg, struct live_ocb *ocb)
{
    int status = iofunc_read_verify(ctp, msg, &ocb->hdr, NULL);
    if (status != EOK) return status;                  /* opened without read access, ... */
    if ((msg->i.xtype & _IO_XTYPE_MASK) != _IO_XTYPE_NONE) return ENOSYS;

    size_t off = (size_t)ocb->hdr.offset;
    size_t left = off < ocb->len ? ocb->len - off : 0;
    size_t nbytes = left < msg->i.nbytes ? left : msg->i.nbytes;

    _IO_SET_READ_NBYTES(ctp, nbytes);                  /* read() will return this */
    if (nbytes == 0) return _RESMGR_NPARTS(0);         /* end of file */
    SETIOV(ctp->iov, ocb->text + off, nbytes);         /* reply straight from our buffer */
    ocb->hdr.offset += nbytes;
    ocb->hdr.attr->hdr.flags |= IOFUNC_ATTR_ATIME;     /* mark the access time for update */
    return _RESMGR_NPARTS(1);                          /* reply with 1 part */
}
```

- The return value is either an `errno` value, which the client sees as `read()` failing with that error, or `_RESMGR_NPARTS(n)`, which means "reply with these `n` buffers".
- `SETIOV` points the reply at your own memory. The kernel copies it straight into the client's buffer, with no extra copy in between.
- The offset lives in the OCB, so two programs reading the file at the same time each have their own position.

### 4. Attach the path and run the loop

```c
dispatch_t *dpp = dispatch_create();
iofunc_func_init(_RESMGR_CONNECT_NFUNCS, &connect_funcs, _RESMGR_IO_NFUNCS, &io_funcs);
io_funcs.read = live_read;                                  /* replace one default */

static iofunc_funcs_t ocb_funcs = {
    .nfuncs = _IOFUNC_NFUNCS,
    .ocb_calloc = live_ocb_calloc,
    .ocb_free = live_ocb_free,
};
static iofunc_mount_t mount = { .funcs = &ocb_funcs };      /* use our OCB functions */

iofunc_attr_init(&uptime.hdr, S_IFREG | 0444, NULL, NULL);  /* a regular, read-only file */
uptime.hdr.mount = &mount;

resmgr_attach(dpp, &rattr, "/dev/livedata/uptime", _FTYPE_ANY, 0,
              &connect_funcs, &io_funcs, &uptime);

for (;;) {
    dispatch_block(ctp);      /* wait for a message (the RECEIVE state) */
    dispatch_handler(ctp);    /* call the right handler; it replies */
}
```

`iofunc_func_init()` fills both tables with the POSIX defaults; you then replace `read`. `resmgr_attach()` registers the path with the process manager. From that moment, opens of `/dev/livedata/uptime` come to this process. The loop handles one message at a time.

## Stop it, and see the path go away
Duration: 0:02

```
# slay -f livedata1
# ls /dev/livedata
ls: /dev/livedata: No such file or directory
```

The path is owned by the process. When the process ends, the process manager removes the path, and no stale entry is left behind. This also happened when we crashed the server on purpose with `slay -s SEGV livedata2`. (`slay -f` doesn't ask for confirmation when more than one process has the name; plain `slay` asks y/N for each.)

Positive
: **What if two copies run?** Start `./livedata1 &` twice, then `cat` the file: on our target it said `opens: 1`, so the newer server answered. Several servers can attach the same path; the `resmgr_attach()` flags `_RESMGR_FLAG_BEFORE` and `_RESMGR_FLAG_AFTER` let a server ask to be resolved before or after the others. Stop both with `slay -f livedata1`.

## Step 2: a file you can write
Duration: 0:10

`livedata2.c` is `livedata1.c` plus a second file, `/dev/livedata/message`, that keeps the last text written to it. Compare the two files to see every change. Here are the important ones.

### A second path from the same process

```c
static struct live_attr msgfile = { .kind = LIVE_MESSAGE };
iofunc_attr_init(&msgfile.hdr, S_IFREG | 0666, NULL, NULL);  /* readable and writable */
msgfile.hdr.mount = &mount;
msgfile.hdr.nbytes = message_len;                           /* the size ls -l reports */
resmgr_attach(dpp, &rattr, "/dev/livedata/message", _FTYPE_ANY, 0,
              &connect_funcs, &io_funcs, &msgfile);
```

The attribute gained a `kind` field, so the shared handlers can tell the two files apart through `ocb->hdr.attr->kind`.

### The write handler

```c
static int live_write(resmgr_context_t *ctp, io_write_t *msg, struct live_ocb *ocb)
{
    int status = iofunc_write_verify(ctp, msg, &ocb->hdr, NULL);
    if (status != EOK) return status;
    if ((msg->i.xtype & _IO_XTYPE_MASK) != _IO_XTYPE_NONE) return ENOSYS;
    if (ocb->hdr.attr->kind != LIVE_MESSAGE) return EROFS;

    size_t nbytes = _IO_WRITE_GET_NBYTES(msg);
    if (nbytes > (size_t)ctp->info.srcmsglen - (size_t)ctp->offset - sizeof(io_write_t))
        return EBADMSG;                              /* claims more data than it sent */

    int append = ocb->wrote || (ocb->hdr.ioflag & O_APPEND);
    size_t start = append ? message_len : 0;
    if (start + nbytes > sizeof(message)) return EFBIG;
    if (resmgr_msgget(ctp, message + start, nbytes, sizeof(msg->i)) == -1) return errno;
    message_len = start + nbytes;
    ...
    _IO_SET_WRITE_NBYTES(ctp, nbytes);              /* write() will return this */
    return _RESMGR_NPARTS(0);                        /* no data in the reply */
}
```

- The data follows the message header. `resmgr_msgget(..., sizeof(msg->i))` reads it from that offset. It copies whatever is already in the receive buffer and fetches the rest from the client if the write was larger. This is the pattern of the SDP 8.0 sample write handler.
- `ocb->hdr.ioflag` holds the `open()` flags plus 1, so `O_APPEND` (the shell's `>>`) can be tested directly.
- Why check the `kind` at all, when `uptime` is mode `0444`? Because `root` may open a read-only file for writing. Without a write handler of your own, the default `iofunc_write_default()` accepts the data and throws it away, as `/dev/null` does. In step 1, `echo hi > /dev/livedata/uptime` "succeeds" as root for exactly this reason.

## Step 2: run it
Duration: 0:05

```
# ./livedata2 &
[1] 13508633
livedata2: serving /dev/livedata/uptime and /dev/livedata/message
# ls -l /dev/livedata
total 0
-rw-rw-rw-  1 root root 20 2026-11-22 20:10 message
-r--r--r--  1 root root  0 2026-11-22 20:10 uptime
# cat /dev/livedata/message
nothing written yet
# echo "hello from the codelab" > /dev/livedata/message
# cat /dev/livedata/message
hello from the codelab
# echo "second line" >> /dev/livedata/message
# cat /dev/livedata/message
hello from the codelab
second line
# ls -l /dev/livedata/message
-rw-rw-rw-  1 root root 35 2026-11-22 20:10 /dev/livedata/message
```

Writing to the read-only file is now refused:

```
# echo hi | cat > /dev/livedata/uptime
cat: xwrite: Read-only file system
# echo $?
1
```

That message is the `EROFS` your handler returned, delivered to `cat` as `write()` failing with `errno` set. (The shell's built-in `echo` fails silently with exit code 1, which is why `cat` is used here.)

Text over 256 bytes in total is refused with `EFBIG` (`File too large`). Stop the server when you're done:

```
# slay -f livedata2
[1] + Terminated           ./livedata2
```

## Extend it
Duration: 0:10

Some ideas to try on your own. They are exercises, not part of the tested example.

- **A file per CPU.** Attach `/dev/livedata/cpu0`, `/dev/livedata/cpu1`, ... and serve each CPU's description from the system page (`_syspage_ptr`, its `cpuinfo` array in `<sys/syspage.h>`).
- **Start at boot.** Call `procmgr_daemon()` so the server detaches from your shell. On a `mkqnximage` target, start it from a startup script.
- **A read that waits.** Reply to a `read()` only when new data arrives: return `_RESMGR_NOREPLY`, keep `ctp->rcvid`, and later call `MsgReply()`. Also handle the unblock message so that a reader can be interrupted with Ctrl-C. qbridge's `qbridged` does this for its `/dev/qbridge/events` file.
- **Commands without text parsing.** Add a `devctl()` handler (`io_funcs.devctl`) for binary commands, as real drivers do.

## Summary and where to go next
Duration: 0:02

You wrote a QNX resource manager:

- `resmgr_attach()` made a path name yours. Every `open()`, `read()`, `write()` and `stat()` on it became a message to your process, and the path vanished when the process ended.
- The iofunc layer handled permissions, offsets and `stat()`. You replaced only `read` and `write`, and extended the attribute and OCB structures with your own fields.
- A snapshot taken in `ocb_calloc` gave each reader a consistent view.

Further reading in the QNX SDP 8.0 documentation:

- *Writing a Resource Manager* (the guide), especially "Handling the _IO_READ message" and "Handling the _IO_WRITE message".
- The C library reference for `resmgr_attach()`, `iofunc_read_verify()`, `iofunc_write_verify()` and `resmgr_msgget()`.

A larger real example: [`qbridged`](https://github.com/vychopade/qbridge/blob/main/target/qbridged/qbridged.c) in this repository serves processes, threads, mutex owners and deadline statistics under `/dev/qbridge`, using the same pattern.
