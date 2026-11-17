CC = qcc
# qbridge_task.h (reporting to qbridged) lives with qbridged.
CFLAGS = -Vgcc_ntox86_64 -g -O2 -Wall -Wextra -std=gnu11 -I../../target/qbridged
LDFLAGS = -Vgcc_ntox86_64 -g -lm

build/rtdemo: rtdemo.c ../../target/qbridged/qbridge_task.h
	@mkdir -p build
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -rf build

.PHONY: clean
