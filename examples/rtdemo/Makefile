CC = qcc
CFLAGS = -Vgcc_ntox86_64 -g -O2 -Wall -Wextra -std=gnu11
LDFLAGS = -Vgcc_ntox86_64 -g -lm

build/rtdemo: rtdemo.c
	@mkdir -p build
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -rf build

.PHONY: clean
