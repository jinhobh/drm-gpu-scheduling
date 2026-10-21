/*
 * burst_client: ground-truth burst source for the attribution study (plan.md, step 1.3a).
 *
 * A Vulkan + Wayland (xdg-shell) window that idles, then renders a burst of N frames at the
 * display rate (FIFO present), and repeats. Every frame is one fragment-shader pass with a
 * fixed GPU cost (work-area pixels x loop iterations), so its exec time on an idle GPU is
 * known. Per frame it logs, in CLOCK_MONOTONIC ns (the clock of trace-cmd -C mono):
 *
 *   t_intended   when the frame should start: the burst start for frame 0, then the arrival of
 *                the previous commit's wl_surface.frame callback (--pace frame, default) or
 *                burst start + frame * refresh (--pace fifo)
 *   t_start      CPU starts the frame (before waiting for a free in-flight slot)
 *   t_acquired   vkAcquireNextImageKHR returned
 *   t_submit     just before vkQueueSubmit  (join key with jobs.csv: pid/tid + time)
 *   t_submit_ret vkQueueSubmit returned
 *   t_present    just before vkQueuePresentKHR (the wl_surface.commit happens inside it)
 *   t_present_ret
 *   gpu_start    vkCmdWriteTimestamp TOP_OF_PIPE, mapped to CLOCK_MONOTONIC with
 *                VK_KHR_calibrated_timestamps (recalibrated at every burst start) plus a
 *                constant correction measured with idle-GPU probes (see probe_offset)
 *   gpu_end      vkCmdWriteTimestamp BOTTOM_OF_PIPE, same mapping
 *   presented    wp_presentation_feedback.presented (the compositor's clock, checked to be
 *                CLOCK_MONOTONIC), with refresh, msc and flags; or discarded
 *
 * The log is a CSV written at exit (SIGINT/SIGTERM or after --bursts). Lines starting with
 * '#' carry the configuration. Burst -1 rows are the startup cost calibration.
 *
 * Calibration mode (--calib) sweeps idle gaps in random order with short bursts, so the
 * exec time of frame 0 after k ms idle can be compared with the warm frames of the same
 * burst (clock ramp-up / GFXOFF wake-up cost).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <getopt.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>
#define VK_USE_PLATFORM_WAYLAND_KHR
#include <vulkan/vulkan.h>

#include "presentation-time-client-protocol.h"
#include "xdg-shell-client-protocol.h"

static const uint32_t vert_spv[] =
#include "burst.vert.inc"
;
static const uint32_t frag_spv[] =
#include "burst.frag.inc"
;

#define MAX_IMAGES 8
#define MAX_INFLIGHT 4
#define MAX_IDLE 32
#define FRAME_CB_TIMEOUT_NS 200000000LL

#define die(...) do { fprintf(stderr, "burst_client: " __VA_ARGS__); fputc('\n', stderr); exit(1); } while (0)
#define VK(call) do { VkResult r_ = (call); if (r_ != VK_SUCCESS) die("%s failed: %d", #call, r_); } while (0)

static volatile sig_atomic_t stop;

static int64_t now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

/* ---- options ---- */

static struct {
	int frames;             /* frames per burst */
	int idle_ms[MAX_IDLE];  /* idle gap before each burst, cycled */
	int n_idle;
	bool shuffle;           /* random order of idle gaps within each round */
	unsigned seed;
	int bursts;             /* 0 = until SIGINT */
	uint32_t iters;         /* 0 = calibrate from cost_us */
	double cost_us;
	double first_mult;      /* frame 0 of a burst costs this many times more */
	int work;               /* side of the square work area, px */
	int inflight;
	bool pace_frame;        /* wait for wl_surface.frame before each frame (else FIFO only) */
	VkPresentModeKHR present;
	bool probe_only;
	int width, height;      /* requested window size (the compositor may change it) */
	const char *out;
} opt = {
	.frames = 30, .idle_ms = {500}, .n_idle = 1, .seed = 1, .cost_us = 2000,
	.first_mult = 1, .work = 512, .inflight = 2, .pace_frame = true,
	.present = VK_PRESENT_MODE_FIFO_KHR, .width = 800, .height = 600,
};

static void usage(void)
{
	fprintf(stderr,
		"usage: burst_client [options]\n"
		"  -n, --frames N        frames per burst (30)\n"
		"  -i, --idle-ms LIST    idle gap(s) before each burst, comma separated, cycled (500)\n"
		"  -s, --shuffle         random order of the idle gaps in each round\n"
		"      --seed N          shuffle seed (1)\n"
		"  -b, --bursts N        number of bursts, 0 = until SIGINT (0)\n"
		"  -c, --cost-us US      GPU cost per frame, calibrated warm at startup (2000)\n"
		"      --iters N         fixed loop iterations instead of --cost-us\n"
		"  -f, --first-mult X    frame 0 of each burst costs X times more (1)\n"
		"  -w, --work PX         side of the square work area in px (512)\n"
		"      --inflight N      frames in flight, 1..%d (2)\n"
		"      --pace MODE       frame: wait for wl_surface.frame callbacks like a toolkit\n"
		"                        (default); fifo: rely on FIFO present only\n"
		"      --present MODE    fifo (default), mailbox or immediate; with --pace fifo and\n"
		"                        --idle-ms 0, mailbox makes an uncapped hog of exact cost\n"
		"      --size WxH        requested window size (800x600)\n"
		"      --calib           preset: idle 1..3000 ms shuffled, 8-frame bursts, 5 rounds\n"
		"      --probe           measure and cache the GPU timestamp correction, then exit\n"
		"                        (exit 1 if the GPU was too busy for a tight probe)\n"
		"  -o, --out FILE        CSV log (stdout)\n", MAX_INFLIGHT);
	exit(2);
}

static void parse_idle(const char *s)
{
	opt.n_idle = 0;
	for (char *end; *s && opt.n_idle < MAX_IDLE; s = *end ? end + 1 : end) {
		opt.idle_ms[opt.n_idle++] = (int)strtol(s, &end, 10);
		if (end == s || (*end && *end != ','))
			die("bad --idle-ms list");
	}
}

static void parse_args(int argc, char **argv)
{
	enum { O_SEED = 256, O_ITERS, O_INFLIGHT, O_SIZE, O_CALIB, O_PACE, O_PRESENT, O_PROBE };
	static const struct option lo[] = {
		{"frames", 1, 0, 'n'}, {"idle-ms", 1, 0, 'i'}, {"shuffle", 0, 0, 's'},
		{"seed", 1, 0, O_SEED}, {"bursts", 1, 0, 'b'}, {"cost-us", 1, 0, 'c'},
		{"iters", 1, 0, O_ITERS}, {"first-mult", 1, 0, 'f'}, {"work", 1, 0, 'w'},
		{"inflight", 1, 0, O_INFLIGHT}, {"size", 1, 0, O_SIZE}, {"calib", 0, 0, O_CALIB},
		{"pace", 1, 0, O_PACE}, {"present", 1, 0, O_PRESENT},
		{"probe", 0, 0, O_PROBE},
		{"out", 1, 0, 'o'}, {"help", 0, 0, 'h'}, {0},
	};
	int c;
	while ((c = getopt_long(argc, argv, "n:i:sb:c:f:w:o:h", lo, NULL)) != -1) {
		switch (c) {
		case 'n': opt.frames = atoi(optarg); break;
		case 'i': parse_idle(optarg); break;
		case 's': opt.shuffle = true; break;
		case O_SEED: opt.seed = (unsigned)atoi(optarg); break;
		case 'b': opt.bursts = atoi(optarg); break;
		case 'c': opt.cost_us = atof(optarg); break;
		case O_ITERS: opt.iters = (uint32_t)atol(optarg); break;
		case 'f': opt.first_mult = atof(optarg); break;
		case 'w': opt.work = atoi(optarg); break;
		case O_INFLIGHT: opt.inflight = atoi(optarg); break;
		case O_SIZE:
			if (sscanf(optarg, "%dx%d", &opt.width, &opt.height) != 2)
				die("bad --size");
			break;
		case O_CALIB:
			parse_idle("1,3,10,30,100,300,1000,3000");
			opt.shuffle = true;
			opt.frames = 8;
			opt.bursts = 5 * opt.n_idle;
			break;
		case O_PACE:
			if (!strcmp(optarg, "frame"))
				opt.pace_frame = true;
			else if (!strcmp(optarg, "fifo"))
				opt.pace_frame = false;
			else
				usage();
			break;
		case O_PRESENT:
			if (!strcmp(optarg, "fifo"))
				opt.present = VK_PRESENT_MODE_FIFO_KHR;
			else if (!strcmp(optarg, "mailbox"))
				opt.present = VK_PRESENT_MODE_MAILBOX_KHR;
			else if (!strcmp(optarg, "immediate"))
				opt.present = VK_PRESENT_MODE_IMMEDIATE_KHR;
			else
				usage();
			break;
		case O_PROBE: opt.probe_only = true; break;
		case 'o': opt.out = optarg; break;
		default: usage();
		}
	}
	if (optind != argc || opt.frames < 1 || opt.work < 1 || opt.inflight < 1 ||
	    opt.inflight > MAX_INFLIGHT || opt.first_mult <= 0)
		usage();
}

/* ---- per-frame records ---- */

enum { PENDING, PRESENTED, DISCARDED };

struct frame {
	int burst, frame, idle_ms, tid;
	int paced;  /* 1 started on a frame callback, -1 callback timed out, 0 not paced */
	uint32_t iters, ext_w, ext_h, area_w, area_h, image;
	int64_t t_intended, t_start, t_acquired, t_submit, t_submit_ret, t_present, t_present_ret;
	int64_t gpu_start, gpu_end, presented;
	uint32_t refresh_ns, flags;
	uint64_t msc;
	int state;
};

static struct frame *frames;
static size_t n_frames, cap_frames;

static size_t new_frame(void)
{
	if (n_frames == cap_frames) {
		cap_frames = cap_frames ? 2 * cap_frames : 4096;
		frames = realloc(frames, cap_frames * sizeof *frames);
		if (!frames)
			die("out of memory");
	}
	memset(&frames[n_frames], 0, sizeof *frames);
	return n_frames++;
}

/* ---- Wayland ---- */

static struct {
	struct wl_display *d;
	struct wl_compositor *comp;
	struct xdg_wm_base *wm;
	struct wp_presentation *pres;
	struct wl_surface *surf;
	struct xdg_surface *xs;
	struct xdg_toplevel *top;
	int w, h;               /* latest configured size */
	bool configured;
	uint32_t pres_clock;
	uint32_t last_refresh;  /* from the latest feedback */
	bool frame_done;        /* wl_surface.frame callback of the last commit arrived */
	int64_t t_frame_done;
} W = {.pres_clock = UINT32_MAX};

static void wm_ping(void *data, struct xdg_wm_base *wm, uint32_t serial)
{
	(void)data;
	xdg_wm_base_pong(wm, serial);
}
static const struct xdg_wm_base_listener wm_listener = {.ping = wm_ping};

static void xs_configure(void *data, struct xdg_surface *xs, uint32_t serial)
{
	(void)data;
	xdg_surface_ack_configure(xs, serial);
	W.configured = true;
}
static const struct xdg_surface_listener xs_listener = {.configure = xs_configure};

static void top_configure(void *data, struct xdg_toplevel *t, int32_t w, int32_t h,
			  struct wl_array *states)
{
	(void)data; (void)t; (void)states;
	if (w > 0 && h > 0) {
		W.w = w;
		W.h = h;
	}
}
static void top_close(void *data, struct xdg_toplevel *t)
{
	(void)data; (void)t;
	stop = 1;
}
static const struct xdg_toplevel_listener top_listener = {
	.configure = top_configure, .close = top_close,
};

static void pres_clock_id(void *data, struct wp_presentation *p, uint32_t clk)
{
	(void)data; (void)p;
	W.pres_clock = clk;
}
static const struct wp_presentation_listener pres_listener = {.clock_id = pres_clock_id};

static void fb_sync_output(void *data, struct wp_presentation_feedback *fb, struct wl_output *o)
{
	(void)data; (void)fb; (void)o;
}
static void fb_presented(void *data, struct wp_presentation_feedback *fb, uint32_t sec_hi,
			 uint32_t sec_lo, uint32_t nsec, uint32_t refresh, uint32_t seq_hi,
			 uint32_t seq_lo, uint32_t flags)
{
	struct frame *f = &frames[(size_t)(uintptr_t)data];
	f->presented = (int64_t)(((uint64_t)sec_hi << 32) | sec_lo) * 1000000000LL + nsec;
	f->refresh_ns = refresh;
	f->msc = ((uint64_t)seq_hi << 32) | seq_lo;
	f->flags = flags;
	f->state = PRESENTED;
	if (refresh)
		W.last_refresh = refresh;
	wp_presentation_feedback_destroy(fb);
}
static void fb_discarded(void *data, struct wp_presentation_feedback *fb)
{
	frames[(size_t)(uintptr_t)data].state = DISCARDED;
	wp_presentation_feedback_destroy(fb);
}
static const struct wp_presentation_feedback_listener fb_listener = {
	.sync_output = fb_sync_output, .presented = fb_presented, .discarded = fb_discarded,
};

static void frame_cb_done(void *data, struct wl_callback *cb, uint32_t ms)
{
	(void)data; (void)ms;
	wl_callback_destroy(cb);
	W.frame_done = true;
	W.t_frame_done = now_ns();
}
static const struct wl_callback_listener frame_cb_listener = {.done = frame_cb_done};

static void reg_global(void *data, struct wl_registry *r, uint32_t name, const char *iface,
		       uint32_t ver)
{
	(void)data; (void)ver;
	if (!strcmp(iface, wl_compositor_interface.name))
		W.comp = wl_registry_bind(r, name, &wl_compositor_interface, 4);
	else if (!strcmp(iface, xdg_wm_base_interface.name))
		W.wm = wl_registry_bind(r, name, &xdg_wm_base_interface, 1);
	else if (!strcmp(iface, wp_presentation_interface.name))
		W.pres = wl_registry_bind(r, name, &wp_presentation_interface, 1);
}
static void reg_remove(void *data, struct wl_registry *r, uint32_t name)
{
	(void)data; (void)r; (void)name;
}
static const struct wl_registry_listener reg_listener = {
	.global = reg_global, .global_remove = reg_remove,
};

/*
 * Dispatch Wayland events, waiting at most until `deadline` (CLOCK_MONOTONIC ns; 0 = don't
 * wait). Mesa's WSI reads the same socket on its own queue; prepare_read makes that safe.
 */
static void pump(int64_t deadline)
{
	while (wl_display_prepare_read(W.d) != 0)
		wl_display_dispatch_pending(W.d);
	wl_display_flush(W.d);
	int64_t left = deadline ? deadline - now_ns() : 0;
	if (left < 0)
		left = 0;
	struct timespec ts = {left / 1000000000LL, left % 1000000000LL};
	struct pollfd p = {wl_display_get_fd(W.d), POLLIN, 0};
	if (ppoll(&p, 1, &ts, NULL) > 0)
		wl_display_read_events(W.d);
	else
		wl_display_cancel_read(W.d);
	wl_display_dispatch_pending(W.d);
}

static void wait_until(int64_t deadline)
{
	while (!stop && now_ns() < deadline)
		pump(deadline);
}

static void wl_init(void)
{
	W.d = wl_display_connect(NULL);
	if (!W.d)
		die("cannot connect to the Wayland display");
	struct wl_registry *reg = wl_display_get_registry(W.d);
	wl_registry_add_listener(reg, &reg_listener, NULL);
	wl_display_roundtrip(W.d);
	if (!W.comp || !W.wm || !W.pres)
		die("compositor lacks wl_compositor, xdg_wm_base or wp_presentation");
	xdg_wm_base_add_listener(W.wm, &wm_listener, NULL);
	wp_presentation_add_listener(W.pres, &pres_listener, NULL);

	W.w = opt.width;
	W.h = opt.height;
	W.surf = wl_compositor_create_surface(W.comp);
	W.xs = xdg_wm_base_get_xdg_surface(W.wm, W.surf);
	xdg_surface_add_listener(W.xs, &xs_listener, NULL);
	W.top = xdg_surface_get_toplevel(W.xs);
	xdg_toplevel_add_listener(W.top, &top_listener, NULL);
	xdg_toplevel_set_title(W.top, "burst_client");
	xdg_toplevel_set_app_id(W.top, "burst_client");
	/* fixed size: Hyprland floats it instead of tiling it next to other windows, so the
	 * work area (clipped to the window) and the frame cost do not depend on the layout */
	xdg_toplevel_set_min_size(W.top, opt.width, opt.height);
	xdg_toplevel_set_max_size(W.top, opt.width, opt.height);
	wl_surface_commit(W.surf);
	while (!W.configured && wl_display_dispatch(W.d) != -1)
		;
	wl_display_roundtrip(W.d);  /* clock_id */
	if (W.pres_clock != CLOCK_MONOTONIC)
		die("presentation clock is %u, not CLOCK_MONOTONIC", W.pres_clock);
}

/* ---- Vulkan ---- */

static struct {
	VkInstance inst;
	VkPhysicalDevice pd;
	VkPhysicalDeviceProperties props;
	VkDevice dev;
	uint32_t qf;
	VkQueue q;
	VkSurfaceKHR surf;
	VkFormat fmt;
	VkRenderPass rp;
	VkPipelineLayout layout;
	VkPipeline pipe;
	VkCommandPool pool;
	VkQueryPool qp;

	VkSwapchainKHR sc;
	VkExtent2D ext;
	uint32_t n_img;
	VkImageView view[MAX_IMAGES];
	VkFramebuffer fb[MAX_IMAGES];
	VkSemaphore rendered[MAX_IMAGES];  /* per image: safe reuse with FIFO */
	bool stale;                        /* swapchain needs recreating */

	VkCommandBuffer cb[MAX_INFLIGHT];
	VkSemaphore acquired[MAX_INFLIGHT];
	VkFence fence[MAX_INFLIGHT];
	ssize_t slot_frame[MAX_INFLIGHT];  /* record whose timestamps are in this slot, -1 */
	uint64_t n_submitted;

	double ns_per_tick;
	uint64_t tick_mask;
	PFN_vkGetCalibratedTimestampsKHR calibrate;
	int64_t cal_mono;
	uint64_t cal_tick;
	uint64_t cal_dev_ns;
	/* vkCmdWriteTimestamp ticks are offset from the calibrated DEVICE domain on this APU */
	int64_t corr_ns, corr_err_ns;
	VkCommandBuffer probe_cb;
	VkFence probe_fence;
} V;

static bool has_ext(const VkExtensionProperties *e, uint32_t n, const char *name)
{
	for (uint32_t i = 0; i < n; i++)
		if (!strcmp(e[i].extensionName, name))
			return true;
	return false;
}

static VkShaderModule shader(const uint32_t *code, size_t size)
{
	VkShaderModuleCreateInfo ci = {
		.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, .codeSize = size, .pCode = code,
	};
	VkShaderModule m;
	VK(vkCreateShaderModule(V.dev, &ci, NULL, &m));
	return m;
}

static void vk_init(void)
{
	const char *inst_ext[] = {VK_KHR_SURFACE_EXTENSION_NAME, VK_KHR_WAYLAND_SURFACE_EXTENSION_NAME};
	VkApplicationInfo app = {
		.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .pApplicationName = "burst_client",
		.apiVersion = VK_API_VERSION_1_1,
	};
	VkInstanceCreateInfo ici = {
		.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app,
		.enabledExtensionCount = 2, .ppEnabledExtensionNames = inst_ext,
	};
	VK(vkCreateInstance(&ici, NULL, &V.inst));

	VkWaylandSurfaceCreateInfoKHR sci = {
		.sType = VK_STRUCTURE_TYPE_WAYLAND_SURFACE_CREATE_INFO_KHR, .display = W.d,
		.surface = W.surf,
	};
	VK(vkCreateWaylandSurfaceKHR(V.inst, &sci, NULL, &V.surf));

	uint32_t n = 8;
	VkPhysicalDevice pds[8];
	vkEnumeratePhysicalDevices(V.inst, &n, pds);
	uint32_t valid_bits = 0;
	for (uint32_t i = 0; i < n && !V.pd; i++) {
		uint32_t nq = 16;
		VkQueueFamilyProperties qp[16];
		vkGetPhysicalDeviceQueueFamilyProperties(pds[i], &nq, qp);
		for (uint32_t j = 0; j < nq; j++) {
			VkBool32 ok = VK_FALSE;
			vkGetPhysicalDeviceSurfaceSupportKHR(pds[i], j, V.surf, &ok);
			if (ok && (qp[j].queueFlags & VK_QUEUE_GRAPHICS_BIT) && qp[j].timestampValidBits) {
				V.pd = pds[i];
				V.qf = j;
				valid_bits = qp[j].timestampValidBits;
				break;
			}
		}
	}
	if (!V.pd)
		die("no Vulkan device can present to this surface with timestamps");
	vkGetPhysicalDeviceProperties(V.pd, &V.props);
	V.ns_per_tick = V.props.limits.timestampPeriod;
	V.tick_mask = valid_bits >= 64 ? UINT64_MAX : (1ULL << valid_bits) - 1;

	uint32_t ne = 0;
	vkEnumerateDeviceExtensionProperties(V.pd, NULL, &ne, NULL);
	VkExtensionProperties *ext = calloc(ne, sizeof *ext);
	vkEnumerateDeviceExtensionProperties(V.pd, NULL, &ne, ext);
	const char *cal_ext, *cal_fn;
	if (has_ext(ext, ne, VK_KHR_CALIBRATED_TIMESTAMPS_EXTENSION_NAME)) {
		cal_ext = VK_KHR_CALIBRATED_TIMESTAMPS_EXTENSION_NAME;
		cal_fn = "vkGetCalibratedTimestampsKHR";
	} else if (has_ext(ext, ne, VK_EXT_CALIBRATED_TIMESTAMPS_EXTENSION_NAME)) {
		cal_ext = VK_EXT_CALIBRATED_TIMESTAMPS_EXTENSION_NAME;
		cal_fn = "vkGetCalibratedTimestampsEXT";
	} else {
		die("no VK_KHR/EXT_calibrated_timestamps");
	}
	free(ext);

	const char *dev_ext[] = {VK_KHR_SWAPCHAIN_EXTENSION_NAME, cal_ext};
	float prio = 1.0f;
	VkDeviceQueueCreateInfo qci = {
		.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = V.qf,
		.queueCount = 1, .pQueuePriorities = &prio,
	};
	VkDeviceCreateInfo dci = {
		.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1,
		.pQueueCreateInfos = &qci, .enabledExtensionCount = 2, .ppEnabledExtensionNames = dev_ext,
	};
	VK(vkCreateDevice(V.pd, &dci, NULL, &V.dev));
	vkGetDeviceQueue(V.dev, V.qf, 0, &V.q);
	V.calibrate = (PFN_vkGetCalibratedTimestampsKHR)vkGetDeviceProcAddr(V.dev, cal_fn);
	if (!V.calibrate)
		die("%s not found", cal_fn);

	uint32_t nf = 64;
	VkSurfaceFormatKHR fmts[64];
	vkGetPhysicalDeviceSurfaceFormatsKHR(V.pd, V.surf, &nf, fmts);
	V.fmt = fmts[0].format;
	for (uint32_t i = 0; i < nf; i++)
		if (fmts[i].format == VK_FORMAT_B8G8R8A8_UNORM)
			V.fmt = fmts[i].format;

	VkAttachmentDescription att = {
		.format = V.fmt, .samples = VK_SAMPLE_COUNT_1_BIT,
		.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR, .storeOp = VK_ATTACHMENT_STORE_OP_STORE,
		.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE,
		.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE,
		.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED, .finalLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR,
	};
	VkAttachmentReference ref = {0, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
	VkSubpassDescription sub = {
		.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS, .colorAttachmentCount = 1,
		.pColorAttachments = &ref,
	};
	VkSubpassDependency dep = {
		.srcSubpass = VK_SUBPASS_EXTERNAL, .dstSubpass = 0,
		.srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
		.dstStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
		.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT,
	};
	VkRenderPassCreateInfo rpci = {
		.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO, .attachmentCount = 1,
		.pAttachments = &att, .subpassCount = 1, .pSubpasses = &sub, .dependencyCount = 1,
		.pDependencies = &dep,
	};
	VK(vkCreateRenderPass(V.dev, &rpci, NULL, &V.rp));

	VkPushConstantRange pcr = {VK_SHADER_STAGE_FRAGMENT_BIT, 0, 2 * sizeof(uint32_t)};
	VkPipelineLayoutCreateInfo plci = {
		.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, .pushConstantRangeCount = 1,
		.pPushConstantRanges = &pcr,
	};
	VK(vkCreatePipelineLayout(V.dev, &plci, NULL, &V.layout));

	VkShaderModule vs = shader(vert_spv, sizeof vert_spv), fs = shader(frag_spv, sizeof frag_spv);
	VkPipelineShaderStageCreateInfo stages[2] = {
		{.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
		 .stage = VK_SHADER_STAGE_VERTEX_BIT, .module = vs, .pName = "main"},
		{.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
		 .stage = VK_SHADER_STAGE_FRAGMENT_BIT, .module = fs, .pName = "main"},
	};
	VkPipelineVertexInputStateCreateInfo vi = {
		.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO,
	};
	VkPipelineInputAssemblyStateCreateInfo ia = {
		.sType = VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO,
		.topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST,
	};
	VkPipelineViewportStateCreateInfo vp = {
		.sType = VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO, .viewportCount = 1,
		.scissorCount = 1,
	};
	VkPipelineRasterizationStateCreateInfo rs = {
		.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO,
		.polygonMode = VK_POLYGON_MODE_FILL, .cullMode = VK_CULL_MODE_NONE, .lineWidth = 1.0f,
	};
	VkPipelineMultisampleStateCreateInfo ms = {
		.sType = VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO,
		.rasterizationSamples = VK_SAMPLE_COUNT_1_BIT,
	};
	VkPipelineColorBlendAttachmentState cba = {.colorWriteMask = 0xf};
	VkPipelineColorBlendStateCreateInfo cb = {
		.sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO, .attachmentCount = 1,
		.pAttachments = &cba,
	};
	VkDynamicState dyn[] = {VK_DYNAMIC_STATE_VIEWPORT, VK_DYNAMIC_STATE_SCISSOR};
	VkPipelineDynamicStateCreateInfo ds = {
		.sType = VK_STRUCTURE_TYPE_PIPELINE_DYNAMIC_STATE_CREATE_INFO, .dynamicStateCount = 2,
		.pDynamicStates = dyn,
	};
	VkGraphicsPipelineCreateInfo gpci = {
		.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO, .stageCount = 2,
		.pStages = stages, .pVertexInputState = &vi, .pInputAssemblyState = &ia,
		.pViewportState = &vp, .pRasterizationState = &rs, .pMultisampleState = &ms,
		.pColorBlendState = &cb, .pDynamicState = &ds, .layout = V.layout, .renderPass = V.rp,
	};
	VK(vkCreateGraphicsPipelines(V.dev, VK_NULL_HANDLE, 1, &gpci, NULL, &V.pipe));
	vkDestroyShaderModule(V.dev, vs, NULL);
	vkDestroyShaderModule(V.dev, fs, NULL);

	VkCommandPoolCreateInfo cpci = {
		.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
		.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT, .queueFamilyIndex = V.qf,
	};
	VK(vkCreateCommandPool(V.dev, &cpci, NULL, &V.pool));
	VkCommandBufferAllocateInfo cbai = {
		.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, .commandPool = V.pool,
		.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = (uint32_t)opt.inflight,
	};
	VK(vkAllocateCommandBuffers(V.dev, &cbai, V.cb));
	VkSemaphoreCreateInfo semci = {.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO};
	VkFenceCreateInfo fci = {
		.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO, .flags = VK_FENCE_CREATE_SIGNALED_BIT,
	};
	for (int i = 0; i < opt.inflight; i++) {
		VK(vkCreateSemaphore(V.dev, &semci, NULL, &V.acquired[i]));
		VK(vkCreateFence(V.dev, &fci, NULL, &V.fence[i]));
		V.slot_frame[i] = -1;
	}
	VkQueryPoolCreateInfo qpci = {
		.sType = VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO, .queryType = VK_QUERY_TYPE_TIMESTAMP,
		.queryCount = 2 * MAX_INFLIGHT + 1,
	};
	VK(vkCreateQueryPool(V.dev, &qpci, NULL, &V.qp));
	cbai.commandBufferCount = 1;
	VK(vkAllocateCommandBuffers(V.dev, &cbai, &V.probe_cb));
	fci.flags = 0;
	VK(vkCreateFence(V.dev, &fci, NULL, &V.probe_fence));
}

static void destroy_swapchain_images(void)
{
	for (uint32_t i = 0; i < V.n_img; i++) {
		vkDestroyFramebuffer(V.dev, V.fb[i], NULL);
		vkDestroyImageView(V.dev, V.view[i], NULL);
		vkDestroySemaphore(V.dev, V.rendered[i], NULL);
	}
	V.n_img = 0;
}

static void make_swapchain(void)
{
	vkDeviceWaitIdle(V.dev);
	destroy_swapchain_images();

	VkSurfaceCapabilitiesKHR caps;
	VK(vkGetPhysicalDeviceSurfaceCapabilitiesKHR(V.pd, V.surf, &caps));
	VkExtent2D ext = caps.currentExtent;
	if (ext.width == UINT32_MAX) {
		ext.width = (uint32_t)W.w;
		ext.height = (uint32_t)W.h;
	}
	uint32_t n = caps.minImageCount < 2 ? 2 : caps.minImageCount;
	if (caps.maxImageCount && n > caps.maxImageCount)
		n = caps.maxImageCount;
	VkSwapchainKHR old = V.sc;
	VkSwapchainCreateInfoKHR ci = {
		.sType = VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR, .surface = V.surf,
		.minImageCount = n, .imageFormat = V.fmt,
		.imageColorSpace = VK_COLOR_SPACE_SRGB_NONLINEAR_KHR, .imageExtent = ext,
		.imageArrayLayers = 1, .imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT,
		.imageSharingMode = VK_SHARING_MODE_EXCLUSIVE, .preTransform = caps.currentTransform,
		.compositeAlpha = VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR,
		.presentMode = opt.present, .clipped = VK_TRUE, .oldSwapchain = old,
	};
	VK(vkCreateSwapchainKHR(V.dev, &ci, NULL, &V.sc));
	if (old)
		vkDestroySwapchainKHR(V.dev, old, NULL);
	V.ext = ext;

	VkImage img[MAX_IMAGES];
	V.n_img = MAX_IMAGES;
	VkResult r = vkGetSwapchainImagesKHR(V.dev, V.sc, &V.n_img, img);
	if (r != VK_SUCCESS)
		die("vkGetSwapchainImagesKHR: %d (more than %d images?)", r, MAX_IMAGES);
	VkSemaphoreCreateInfo semci = {.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO};
	for (uint32_t i = 0; i < V.n_img; i++) {
		VkImageViewCreateInfo vci = {
			.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .image = img[i],
			.viewType = VK_IMAGE_VIEW_TYPE_2D, .format = V.fmt,
			.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1},
		};
		VK(vkCreateImageView(V.dev, &vci, NULL, &V.view[i]));
		VkFramebufferCreateInfo fci = {
			.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO, .renderPass = V.rp,
			.attachmentCount = 1, .pAttachments = &V.view[i], .width = ext.width,
			.height = ext.height, .layers = 1,
		};
		VK(vkCreateFramebuffer(V.dev, &fci, NULL, &V.fb[i]));
		VK(vkCreateSemaphore(V.dev, &semci, NULL, &V.rendered[i]));
	}
	V.stale = false;
}

/* Map GPU ticks to CLOCK_MONOTONIC around a fresh (tick, mono) pair. */
static void calibrate_clocks(void)
{
	VkCalibratedTimestampInfoKHR info[2] = {
		{.sType = VK_STRUCTURE_TYPE_CALIBRATED_TIMESTAMP_INFO_KHR,
		 .timeDomain = VK_TIME_DOMAIN_DEVICE_KHR},
		{.sType = VK_STRUCTURE_TYPE_CALIBRATED_TIMESTAMP_INFO_KHR,
		 .timeDomain = VK_TIME_DOMAIN_CLOCK_MONOTONIC_KHR},
	};
	uint64_t ts[2], dev;
	VK(V.calibrate(V.dev, 2, info, ts, &dev));
	V.cal_tick = ts[0];
	V.cal_mono = (int64_t)ts[1];
	V.cal_dev_ns = dev;
}

static int64_t tick_to_mono(uint64_t tick)
{
	uint64_t d = (tick - V.cal_tick) & V.tick_mask;
	int64_t sd = (int64_t)d;
	if (V.tick_mask != UINT64_MAX && d > V.tick_mask / 2)
		sd -= (int64_t)V.tick_mask + 1;
	return V.cal_mono + (int64_t)llround((double)sd * V.ns_per_tick) + V.corr_ns;
}

/*
 * Measure the offset between command-stream timestamps and the calibrated DEVICE domain:
 * a timestamp-only submit on an idle GPU must land between the CPU times before the
 * submit and after its fence. Intersect those intervals over n probes; the correction is
 * the midpoint, the error half the width.
 */
static void probe_offset(int n, int64_t *corr, int64_t *err, int64_t *lo_out, int64_t *hi_out)
{
	int64_t lo = INT64_MIN, hi = INT64_MAX;
	uint32_t q = 2 * MAX_INFLIGHT;
	int64_t saved = V.corr_ns;
	V.corr_ns = 0;
	calibrate_clocks();
	for (int i = 0; i < n; i++) {
		VkCommandBufferBeginInfo bi = {
			.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
			.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT,
		};
		VK(vkResetCommandBuffer(V.probe_cb, 0));
		VK(vkBeginCommandBuffer(V.probe_cb, &bi));
		vkCmdResetQueryPool(V.probe_cb, V.qp, q, 1);
		vkCmdWriteTimestamp(V.probe_cb, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, V.qp, q);
		VK(vkEndCommandBuffer(V.probe_cb));
		VkSubmitInfo si = {
			.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1,
			.pCommandBuffers = &V.probe_cb,
		};
		int64_t t0 = now_ns();
		VK(vkQueueSubmit(V.q, 1, &si, V.probe_fence));
		VK(vkWaitForFences(V.dev, 1, &V.probe_fence, VK_TRUE, UINT64_MAX));
		int64_t t1 = now_ns();
		VK(vkResetFences(V.dev, 1, &V.probe_fence));
		uint64_t tick;
		VK(vkGetQueryPoolResults(V.dev, V.qp, q, 1, sizeof tick, &tick, sizeof tick,
					 VK_QUERY_RESULT_64_BIT | VK_QUERY_RESULT_WAIT_BIT));
		int64_t g = tick_to_mono(tick);
		if (t0 - g > lo)
			lo = t0 - g;
		if (t1 - g < hi)
			hi = t1 - g;
		struct timespec ts = {0, 2000000};  /* let the GPU go idle between probes */
		nanosleep(&ts, NULL);
	}
	V.corr_ns = saved;
	if (lo > hi)
		die("timestamp probes disagree: [%lld, %lld]", (long long)lo, (long long)hi);
	*corr = (lo + hi) / 2;
	*err = (hi - lo) / 2;
	if (lo_out) {
		*lo_out = lo;
		*hi_out = hi;
	}
}

/*
 * The correction is constant within a boot, but probes are loose while another client keeps
 * the ring busy. Keep a tight probe in $XDG_RUNTIME_DIR and fall back to it when the live
 * probe is loose and agrees with it.
 */
#define GOOD_PROBE_NS 100000
static void corr_path(char *buf, size_t n)
{
	const char *rt = getenv("XDG_RUNTIME_DIR");
	snprintf(buf, n, "%s/burst_client-ts-corr", rt ? rt : "/tmp");
}

static void boot_id(char *buf, size_t n)
{
	FILE *f = fopen("/proc/sys/kernel/random/boot_id", "r");
	buf[0] = 0;
	if (f) {
		if (!fgets(buf, (int)n, f))
			buf[0] = 0;
		fclose(f);
	}
	buf[strcspn(buf, "\n")] = 0;
}

static void measure_correction(const char **source)
{
	int64_t lo, hi;
	char path[512], id[64], cid[64];
	corr_path(path, sizeof path);
	boot_id(id, sizeof id);
	probe_offset(50, &V.corr_ns, &V.corr_err_ns, &lo, &hi);
	*source = "probe";
	if (V.corr_err_ns <= GOOD_PROBE_NS) {
		FILE *f = fopen(path, "w");
		if (f) {
			fprintf(f, "%s %lld %lld\n", id, (long long)V.corr_ns, (long long)V.corr_err_ns);
			fclose(f);
		}
		return;
	}
	long long c, e;
	FILE *f = fopen(path, "r");
	if (!f)
		return;
	if (fscanf(f, "%63s %lld %lld", cid, &c, &e) == 3 && !strcmp(cid, id) && c >= lo && c <= hi) {
		V.corr_ns = c;
		V.corr_err_ns = e;
		*source = "cached";
	}
	fclose(f);
}

/* Read the timestamps of the frame that last used `slot` (its fence has signalled). */
static void collect(int slot)
{
	ssize_t i = V.slot_frame[slot];
	if (i < 0)
		return;
	uint64_t t[2];
	VK(vkGetQueryPoolResults(V.dev, V.qp, 2 * (uint32_t)slot, 2, sizeof t, t, sizeof t[0],
				 VK_QUERY_RESULT_64_BIT | VK_QUERY_RESULT_WAIT_BIT));
	frames[i].gpu_start = tick_to_mono(t[0]);
	frames[i].gpu_end = tick_to_mono(t[1]);
	V.slot_frame[slot] = -1;
}

static void drain(void)
{
	for (int s = 0; s < opt.inflight; s++) {
		VK(vkWaitForFences(V.dev, 1, &V.fence[s], VK_TRUE, UINT64_MAX));
		collect(s);
	}
}

static void render(size_t idx)
{
	int slot = (int)(V.n_submitted % (uint64_t)opt.inflight);
	frames[idx].t_start = now_ns();
	VK(vkWaitForFences(V.dev, 1, &V.fence[slot], VK_TRUE, UINT64_MAX));
	collect(slot);

	if (V.stale || (int)V.ext.width != W.w || (int)V.ext.height != W.h)
		make_swapchain();
	uint32_t img;
	VkResult r;
	while ((r = vkAcquireNextImageKHR(V.dev, V.sc, UINT64_MAX, V.acquired[slot], VK_NULL_HANDLE,
					  &img)) == VK_ERROR_OUT_OF_DATE_KHR)
		make_swapchain();
	if (r != VK_SUCCESS && r != VK_SUBOPTIMAL_KHR)
		die("vkAcquireNextImageKHR: %d", r);
	VK(vkResetFences(V.dev, 1, &V.fence[slot]));

	struct frame *f = &frames[idx];
	f->t_acquired = now_ns();
	f->image = img;
	f->ext_w = V.ext.width;
	f->ext_h = V.ext.height;
	f->area_w = V.ext.width < (uint32_t)opt.work ? V.ext.width : (uint32_t)opt.work;
	f->area_h = V.ext.height < (uint32_t)opt.work ? V.ext.height : (uint32_t)opt.work;

	VkCommandBuffer cb = V.cb[slot];
	VkCommandBufferBeginInfo bi = {
		.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
		.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT,
	};
	VK(vkResetCommandBuffer(cb, 0));
	VK(vkBeginCommandBuffer(cb, &bi));
	vkCmdResetQueryPool(cb, V.qp, 2 * (uint32_t)slot, 2);
	vkCmdWriteTimestamp(cb, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, V.qp, 2 * (uint32_t)slot);
	VkClearValue clear = {.color = {{0.05f, 0.05f, 0.08f, 1.0f}}};
	VkRenderPassBeginInfo rpbi = {
		.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO, .renderPass = V.rp,
		.framebuffer = V.fb[img], .renderArea = {{0, 0}, V.ext}, .clearValueCount = 1,
		.pClearValues = &clear,
	};
	vkCmdBeginRenderPass(cb, &rpbi, VK_SUBPASS_CONTENTS_INLINE);
	vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, V.pipe);
	VkViewport vp = {0, 0, (float)f->area_w, (float)f->area_h, 0, 1};
	VkRect2D sc = {{0, 0}, {f->area_w, f->area_h}};
	vkCmdSetViewport(cb, 0, 1, &vp);
	vkCmdSetScissor(cb, 0, 1, &sc);
	uint32_t pc[2] = {f->iters, (uint32_t)idx};
	vkCmdPushConstants(cb, V.layout, VK_SHADER_STAGE_FRAGMENT_BIT, 0, sizeof pc, pc);
	vkCmdDraw(cb, 3, 1, 0, 0);
	vkCmdEndRenderPass(cb);
	vkCmdWriteTimestamp(cb, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, V.qp, 2 * (uint32_t)slot + 1);
	VK(vkEndCommandBuffer(cb));

	VkPipelineStageFlags wait = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
	VkSubmitInfo si = {
		.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .waitSemaphoreCount = 1,
		.pWaitSemaphores = &V.acquired[slot], .pWaitDstStageMask = &wait,
		.commandBufferCount = 1, .pCommandBuffers = &cb, .signalSemaphoreCount = 1,
		.pSignalSemaphores = &V.rendered[img],
	};
	f->tid = gettid();
	f->t_submit = now_ns();
	VK(vkQueueSubmit(V.q, 1, &si, V.fence[slot]));
	f->t_submit_ret = now_ns();
	V.slot_frame[slot] = (ssize_t)idx;
	V.n_submitted++;

	/* feedback and frame callback attach to the next commit, which Mesa makes inside
	 * vkQueuePresentKHR */
	W.frame_done = false;
	wl_callback_add_listener(wl_surface_frame(W.surf), &frame_cb_listener, NULL);
	struct wp_presentation_feedback *fb = wp_presentation_feedback(W.pres, W.surf);
	wp_presentation_feedback_add_listener(fb, &fb_listener, (void *)(uintptr_t)idx);
	VkPresentInfoKHR pi = {
		.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR, .waitSemaphoreCount = 1,
		.pWaitSemaphores = &V.rendered[img], .swapchainCount = 1, .pSwapchains = &V.sc,
		.pImageIndices = &img,
	};
	f->t_present = now_ns();
	r = vkQueuePresentKHR(V.q, &pi);
	f->t_present_ret = now_ns();
	if (r == VK_ERROR_OUT_OF_DATE_KHR || r == VK_SUBOPTIMAL_KHR)
		V.stale = true;
	else if (r != VK_SUCCESS)
		die("vkQueuePresentKHR: %d", r);
	pump(0);
}

static int64_t refresh_ns(void)
{
	return W.last_refresh ? W.last_refresh : 16666667;
}

static void run_burst(int burst, int idle_ms, uint32_t iters, int n)
{
	calibrate_clocks();
	int64_t t0 = now_ns(), period = refresh_ns();
	for (int i = 0; i < n && !stop; i++) {
		int64_t intended = t0 + i * period;
		int paced = 0;
		if (i > 0 && opt.pace_frame) {
			/* like a toolkit frame clock: draw the next frame when the compositor says */
			int64_t deadline = now_ns() + FRAME_CB_TIMEOUT_NS;
			while (!stop && !W.frame_done && now_ns() < deadline)
				pump(deadline);
			paced = W.frame_done ? 1 : -1;
			intended = W.frame_done ? W.t_frame_done : now_ns();
		}
		size_t idx = new_frame();
		struct frame *f = &frames[idx];
		f->burst = burst;
		f->frame = i;
		f->idle_ms = idle_ms;
		f->paced = paced;
		f->iters = i == 0 && burst >= 0 ? (uint32_t)llround(iters * opt.first_mult) : iters;
		f->t_intended = intended;
		render(idx);
	}
	drain();
}

static int cmp_i64(const void *a, const void *b)
{
	int64_t x = *(const int64_t *)a, y = *(const int64_t *)b;
	return (x > y) - (x < y);
}

/* Warm, continuous rendering; scale iters until the median exec time hits cost_us. */
static uint32_t calibrate_iters(void)
{
	uint32_t iters = 2000;
	for (int round = 0; round < 4 && !stop; round++) {
		size_t first = n_frames;
		run_burst(-1, 0, iters, 40);
		int64_t ex[40];
		int m = 0;
		for (size_t i = first + 10; i < n_frames; i++)
			ex[m++] = frames[i].gpu_end - frames[i].gpu_start;
		if (!m)
			break;
		qsort(ex, (size_t)m, sizeof ex[0], cmp_i64);
		double med_us = ex[m / 2] / 1000.0;
		fprintf(stderr, "burst_client: calibration: iters=%u exec median %.1f us\n", iters, med_us);
		double scale = opt.cost_us / (med_us > 1 ? med_us : 1);
		iters = (uint32_t)fmax(1, llround(iters * scale));
		if (fabs(scale - 1) < 0.03)
			break;
	}
	return iters;
}

static void on_signal(int sig)
{
	(void)sig;
	stop = 1;
}

static void write_log(FILE *o, uint32_t iters, const char *corr_src, int64_t end_corr,
		      int64_t end_err)
{
	fprintf(o, "# burst_client pid=%d device=\"%s\" driver=0x%x\n", getpid(),
		V.props.deviceName, V.props.driverVersion);
	fprintf(o, "# iters=%u cost_us=%.0f%s first_mult=%.2f work=%d inflight=%d frames=%d "
		"bursts=%d shuffle=%d seed=%u idle_ms=", iters, opt.cost_us,
		opt.iters ? "(fixed iters)" : "", opt.first_mult, opt.work, opt.inflight, opt.frames,
		opt.bursts, opt.shuffle, opt.seed);
	for (int i = 0; i < opt.n_idle; i++)
		fprintf(o, "%s%d", i ? "," : "", opt.idle_ms[i]);
	static const char *pm[] = {"immediate", "mailbox", "fifo"};
	fprintf(o, " pace=%s present=%s\n", opt.pace_frame ? "frame" : "fifo", pm[opt.present]);
	fprintf(o, "# clock=CLOCK_MONOTONIC ns_per_tick=%.4f tick_mask=0x%llx last_cal_dev_ns=%llu "
		"ts_corr_ns=%lld+-%lld(%s) end_ts_corr_ns=%lld+-%lld\n", V.ns_per_tick,
		(unsigned long long)V.tick_mask, (unsigned long long)V.cal_dev_ns,
		(long long)V.corr_ns, (long long)V.corr_err_ns, corr_src, (long long)end_corr,
		(long long)end_err);
	fprintf(o, "burst,frame,idle_ms,paced,iters,tid,image,ext_w,ext_h,area_w,area_h,"
		"t_intended,t_start,t_acquired,t_submit,t_submit_ret,t_present,t_present_ret,"
		"gpu_start,gpu_end,exec_us,presented,refresh_ns,msc,flags,state\n");
	static const char *state[] = {"pending", "presented", "discarded"};
	for (size_t i = 0; i < n_frames; i++) {
		struct frame *f = &frames[i];
		fprintf(o, "%d,%d,%d,%d,%u,%d,%u,%u,%u,%u,%u,%lld,%lld,%lld,%lld,%lld,%lld,%lld,%lld,%lld,"
			"%.1f,%lld,%u,%llu,%u,%s\n", f->burst, f->frame, f->idle_ms, f->paced, f->iters, f->tid,
			f->image, f->ext_w, f->ext_h, f->area_w, f->area_h, (long long)f->t_intended,
			(long long)f->t_start, (long long)f->t_acquired, (long long)f->t_submit,
			(long long)f->t_submit_ret, (long long)f->t_present, (long long)f->t_present_ret,
			(long long)f->gpu_start, (long long)f->gpu_end,
			(f->gpu_end - f->gpu_start) / 1000.0, (long long)f->presented, f->refresh_ns,
			(unsigned long long)f->msc, f->flags, state[f->state]);
	}
	if (o != stdout)
		fclose(o);
}

int main(int argc, char **argv)
{
	parse_args(argc, argv);
	/* open the log now: finding out it can't be written only at exit loses the run */
	FILE *log = stdout;
	if (opt.out && !opt.probe_only && !(log = fopen(opt.out, "w")))
		die("cannot write %s: %s", opt.out, strerror(errno));
	struct sigaction sa = {.sa_handler = on_signal};  /* no SA_RESTART: ppoll returns */
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);

	wl_init();
	vk_init();
	make_swapchain();
	fprintf(stderr, "burst_client: %s, %ux%u, period %.3f ns/tick\n", V.props.deviceName,
		V.ext.width, V.ext.height, V.ns_per_tick);

	const char *corr_src;
	measure_correction(&corr_src);
	fprintf(stderr, "burst_client: timestamp correction %.3f ms +- %.1f us (%s)\n",
		V.corr_ns / 1e6, V.corr_err_ns / 1e3, corr_src);
	if (V.corr_err_ns > GOOD_PROBE_NS)
		fprintf(stderr, "burst_client: warning: loose timestamp correction; run "
			"burst_client --probe on an idle GPU first\n");
	if (opt.probe_only)
		return V.corr_err_ns > GOOD_PROBE_NS;
	uint32_t iters = opt.iters ? opt.iters : calibrate_iters();
	fprintf(stderr, "burst_client: iters=%u\n", iters);

	srand(opt.seed);
	int order[MAX_IDLE];
	for (int i = 0; i < opt.n_idle; i++)
		order[i] = i;
	for (int b = 0; !stop && (opt.bursts == 0 || b < opt.bursts); b++) {
		int k = b % opt.n_idle;
		if (k == 0 && opt.shuffle)
			for (int i = opt.n_idle - 1; i > 0; i--) {
				int j = rand() % (i + 1), t = order[i];
				order[i] = order[j];
				order[j] = t;
			}
		int idle = opt.idle_ms[order[k]];
		wait_until(now_ns() + idle * 1000000LL);
		if (stop)
			break;
		run_burst(b, idle, iters, opt.frames);
	}

	/* let the last presentation feedback arrive */
	int64_t end_corr, end_err;
	vkDeviceWaitIdle(V.dev);
	drain();
	stop = 0;
	wait_until(now_ns() + 300000000LL);
	probe_offset(50, &end_corr, &end_err, NULL, NULL);
	write_log(log, iters, corr_src, end_corr, end_err);

	vkDeviceWaitIdle(V.dev);
	destroy_swapchain_images();
	vkDestroySwapchainKHR(V.dev, V.sc, NULL);
	vkDestroySurfaceKHR(V.inst, V.surf, NULL);
	vkDestroyDevice(V.dev, NULL);
	vkDestroyInstance(V.inst, NULL);
	wl_display_disconnect(W.d);
	return 0;
}
