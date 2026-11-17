# Demo apps

Three small QNX programs, each with one planted bug. They are what the qbridge demo uses: the assistant has to find each bug from what qbridge shows it about the running target, not by reading the code first.

Build, deploy and run any of them with qbridge (`--allow-write` is needed to deploy and run):

```
qbridge build examples\<name>
qbridge --allow-write deploy examples\<name>\build\<name>
```

## crash_null: NULL pointer dereference

`sensor.c` looks up a calibration entry and does not check for "not found". Sensor 7 is unknown, so it crashes with SIGSEGV.

```
qbridge --allow-write run sensor          # "signal": "SIGSEGV", hint to use qnx_crash
qbridge crash                             # backtrace, locals, summary
```

qbridge reports: *died with SIGSEGV in apply_calibration() at sensor.c:38. Fault address 0x8 is in the first page: a NULL pointer dereference (field at offset 8). NULL pointer(s) in that frame: cal.*

## inversion: priority inversion over message passing

`inversion.c` runs a server (priority 10), a client (30) and a CPU hog (20) on CPU 0. The server's channel is created with `_NTO_CHF_FIXED_PRIORITY`, which turns off QNX's priority inheritance, so once the hog starts the client waits behind it.

```
qbridge --allow-write run --background --timeout 40 inversion 10
qbridge ps --process inversion            # look at "inversions"
```

qbridge reports: *inversion thread 'client-high' (priority 30) is waiting on inversion thread 'server-low', which runs at priority 10: the server did not inherit the client's priority. It is being held off by inversion thread 'hog-medium' at priority 20 (RUNNING). This is a priority inversion. ... check for _NTO_CHF_FIXED_PRIORITY on the server's channel.*

Measured on the target: with the bug the worst request took about 6–8 s (expected about 20 ms). With `ChannelCreate(0)` instead, the worst was 28 ms and qbridge reports no inversion.

## deadlock: message-passing deadlock

`deadlock.c` has two processes. `config` asks `sensor` for a reading; to answer, `sensor` asks `config` for its calibration, but `config`'s only thread is still waiting for `sensor`'s reply.

```
qbridge --allow-write run --background --timeout 60 deadlock
qbridge ps --process deadlock             # look at "deadlocks"
qbridge --allow-write kill --name deadlock --signal ABRT   # dumper writes a core
qbridge crash --core deadlock             # where it was stuck
```

qbridge reports: *Deadlock: deadlock thread 'sensor-main' is SEND-blocked on config; deadlock thread 'config-main' is REPLY-blocked on sensor. Each is waiting for the other, so none can move.* The core from `kill --signal ABRT` shows *in MsgSend(), called from run_sensor() at deadlock.c:64*, the call back into `config`.

Fixing it (for example, `sensor` using a calibration it already has instead of calling back) makes the cycle disappear.

## hello

Prints its pid, CPU and arguments. Used to check build → deploy → run.
