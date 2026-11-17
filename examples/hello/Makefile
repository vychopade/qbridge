CC = qcc
CFLAGS = -Vgcc_ntox86_64 -g -Wall -Wextra
LDFLAGS = -Vgcc_ntox86_64 -g

build/hello: hello.c
	@mkdir -p build
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -rf build

.PHONY: clean
