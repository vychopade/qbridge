CC = qcc
CFLAGS = -Vgcc_ntox86_64 -g -O0 -Wall -Wextra
LDFLAGS = -Vgcc_ntox86_64 -g

build/inversion: inversion.c
	@mkdir -p build
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -rf build

.PHONY: clean
