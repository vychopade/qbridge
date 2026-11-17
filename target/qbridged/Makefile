CC = qcc
CFLAGS = -Vgcc_ntox86_64 -g -O2 -Wall -Wextra -std=gnu11
LDFLAGS = -Vgcc_ntox86_64 -g -lm

SRCS = qbridged.c qb_deadlines.c
HDRS = qb_sbuf.h qb_deadlines.h qbridge_task.h

build/qbridged: $(SRCS) $(HDRS)
	@mkdir -p build
	$(CC) $(CFLAGS) -o $@ $(SRCS) $(LDFLAGS)

clean:
	rm -rf build

.PHONY: clean
