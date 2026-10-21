# burst_client: plain Makefile (no meson/ninja on this machine).
# Needs: vulkan-headers, wayland, wayland-protocols, shaderc (glslc).
CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra
CFLAGS += -std=gnu11 $(VK_CFLAGS)
PROTO := $(shell pkg-config --variable=pkgdatadir wayland-protocols)
LIBS := $(shell pkg-config --libs wayland-client vulkan) -lm

GEN := xdg-shell-client-protocol.h xdg-shell-protocol.c \
       presentation-time-client-protocol.h presentation-time-protocol.c \
       burst.vert.inc burst.frag.inc

all: burst_client

burst_client: main.c $(GEN)
	$(CC) $(CFLAGS) -I. -o $@ main.c xdg-shell-protocol.c presentation-time-protocol.c $(LIBS)

xdg-shell-client-protocol.h: $(PROTO)/stable/xdg-shell/xdg-shell.xml
	wayland-scanner client-header $< $@
xdg-shell-protocol.c: $(PROTO)/stable/xdg-shell/xdg-shell.xml
	wayland-scanner private-code $< $@
presentation-time-client-protocol.h: $(PROTO)/stable/presentation-time/presentation-time.xml
	wayland-scanner client-header $< $@
presentation-time-protocol.c: $(PROTO)/stable/presentation-time/presentation-time.xml
	wayland-scanner private-code $< $@

# glslc -mfmt=c writes a C initializer list
%.inc: %
	glslc -O -mfmt=c -o $@ $<

clean:
	rm -f burst_client $(GEN)

.PHONY: all clean
