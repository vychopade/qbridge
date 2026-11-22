# Builds both steps of the codelab for an x86_64 QNX 8.0 target.
# Run "make" in a shell where qnxsdp-env has been set up.
CC = qcc
CFLAGS = -Vgcc_ntox86_64 -g -Wall -Wextra

all: livedata1 livedata2

livedata1: livedata1.c
	$(CC) $(CFLAGS) -o $@ $<

livedata2: livedata2.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f livedata1 livedata2

.PHONY: all clean
