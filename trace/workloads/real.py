#!/usr/bin/env python3
"""Scripted real victims; run directly for a pilot or under capture.sh for tracing.

See trace/reports/real-study-protocol.md for measurement limits and capture commands.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent


def hypr(query):
    return json.loads(subprocess.check_output(["hyprctl", query, "-j"], text=True))


def focus(workspace):
    subprocess.run(["hyprctl", "dispatch",
                    f"hl.dsp.focus({{ workspace = {workspace} }})"], check=True,
                   stdout=subprocess.DEVNULL)


def stop(process):
    # Every owned process has its own session, including Firefox's child processes.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            continue
        # The leader may exit before a child. Still terminate the owned group.
    process.wait()


def cpu_worker():
    value = b"drm-sched-cpu-control"
    while True:
        value = hashlib.sha256(value).digest()


class Run:
    def __init__(self, out):
        self.out = out
        self.processes = []
        self.events = (out / "events.jsonl").open("x", buffering=1)

    def event(self, kind, **data):
        self.events.write(json.dumps(dict(t_ns=time.monotonic_ns(), event=kind, **data)) + "\n")

    def start(self, label, argv, env=None):
        self.event("spawn_request", label=label, argv=argv)
        with (self.out / f"{label}.log").open("w") as log:
            p = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True, env=env)
        self.processes.append(p)
        self.event("spawn_return", label=label, pid=p.pid)
        return p

    def mapped(self, process, title=None):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"victim exited before mapping: {process.returncode}")
            clients = hypr("clients")
            matches = [c for c in clients if c.get("mapped") and
                       (c.get("pid") == process.pid or
                        (title is not None and c.get("title") == title))]
            if matches:
                self.event("map_observed", pid=process.pid, clients=matches)
                return matches[0]
            time.sleep(.02)
        raise RuntimeError("victim did not map within 20 seconds")

    def snapshot(self, label):
        pressure = Path("/proc/pressure/cpu")
        self.event("snapshot", label=label, clients=hypr("clients"), monitors=hypr("monitors"),
                   cpu_pressure=pressure.read_text() if pressure.exists() else None)

    def finish(self, process):
        stop(process)
        self.processes.remove(process)

    def close(self):
        for process in reversed(self.processes):
            stop(process)
        self.events.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scenario", choices=["gtk-launch", "firefox-scroll", "compositor", "mpv"])
    ap.add_argument("--hog", default="none")
    ap.add_argument("--cpu-workers", type=int, default=0)
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--idle", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=2.0,
                    help="seconds per trial (mpv uses duration * trials continuous playback)")
    ap.add_argument("--clip", type=Path, help="local mpv clip; never a network URL")
    ap.add_argument("--load-clip", type=Path, help="local clip for --hog mpv")
    ap.add_argument("--out", type=Path, default=Path(os.environ.get("CAPTURE_OUT", ".")))
    ap.add_argument("--check", action="store_true", help="validate prerequisites without opening windows")
    args = ap.parse_args()
    names = subprocess.check_output([str(HERE / "hog.sh"), "--names"], text=True).split()
    if args.hog not in names:
        ap.error(f"unknown hog; choose from {names}")
    if args.trials < 1 or args.duration <= 0 or args.idle < 0 or args.cpu_workers < 0:
        ap.error("trials/duration must be positive; idle/cpu-workers must be nonnegative")
    if args.scenario == "compositor" and args.hog not in ("none", "gl-hog"):
        ap.error("workspace animations require none or off-screen gl-hog (visibility control)")
    if args.hog == "mpv" and (args.load_clip is None or not args.load_clip.is_file()):
        ap.error("mpv hog needs --load-clip pointing to a local file")
    if args.scenario == "mpv" and (args.clip is None or not args.clip.is_file()):
        ap.error("mpv requires --clip pointing to a local file")
    required = ["hyprctl"] + {
        "gtk-launch": ["zenity"], "firefox-scroll": ["firefox", "wtype"],
        "compositor": ["zenity"], "mpv": ["mpv"],
    }[args.scenario]
    required += {"gl-hog": ["glmark2-wayland"], "glmark2-terrain": ["glmark2-wayland"],
                 "glmark2-refract": ["glmark2-wayland"], "stk": ["supertuxkart"],
                 "vkmark-cube": ["vkmark"], "mpv": ["mpv"]}.get(args.hog, [])
    if args.hog in ("vk-hog", "vk-hog1") and not os.access(HERE.parent / "burst_client/burst_client", os.X_OK):
        ap.error("build the Vulkan hog with make -C trace/burst_client")
    for command in required:
        if not shutil.which(command):
            ap.error(f"missing command: {command}")
    monitors = hypr("monitors")
    if len(monitors) != 1 or monitors[0]["name"] != "eDP-1":
        ap.error("requires eDP-1 as the only active monitor; unplug/disable DP-1 first")
    if args.check:
        print("prerequisites OK; no workload started")
        return

    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    original = hypr("activeworkspace")["id"]
    # Allocate two unused regular workspace IDs. Never rearrange an existing workspace.
    workspace = max([w["id"] for w in hypr("workspaces")] + [0]) + 1
    run = Run(args.out)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    try:
        run.event("settings", arguments={k: str(v) if isinstance(v, Path) else v
                                       for k, v in vars(args).items()}, monitors=monitors,
                  cpu_affinity=sorted(os.sched_getaffinity(0)))
        focus(workspace)
        run.snapshot("empty-workspace")
        hog = None
        if args.hog != "none":
            env = dict(os.environ, HOG_LOG=str(args.out / "hog.csv"),
                       MPV_LOG=str(args.out / "mpv-load.jsonl"))
            if args.load_clip:
                clip = args.load_clip.resolve()
                env["MPV_CLIP"] = str(clip)
                with clip.open("rb") as f:
                    run.event("load_clip", path=str(clip), sha256=hashlib.file_digest(f, "sha256").hexdigest())
            hog = run.start("hog", [str(HERE / "hog.sh"), args.hog], env)
        for i in range(args.cpu_workers):
            run.start(f"cpu-{i}", [sys.executable, str(Path(__file__).resolve()), "--cpu-worker"])
        time.sleep(3)
        if hog is not None and hog.poll() is not None:
            raise RuntimeError("hog exited during warmup")
        page = HERE / "scroll.html"
        browser = None
        if args.scenario == "firefox-scroll":
            profile = args.out / "firefox-profile"
            profile.mkdir()
            (profile / "user.js").write_text('user_pref("browser.shell.checkDefaultBrowser", false);\n'
                'user_pref("browser.startup.homepage_override.mstone", "ignore");\n'
                'user_pref("browser.aboutwelcome.enabled", false);\n'
                'user_pref("general.smoothScroll", true);\n')
            browser = run.start("firefox", ["firefox", "--new-instance", "--profile", str(profile),
                                          "--kiosk", page.as_uri()],
                                dict(os.environ, MOZ_ENABLE_WAYLAND="1"))
            run.mapped(browser, "DRM scheduling scroll fixture")
            time.sleep(3)
        if args.scenario == "mpv":
            clip = args.clip.resolve()
            with clip.open("rb") as f:
                run.event("clip", path=str(clip), sha256=hashlib.file_digest(f, "sha256").hexdigest())
            env = dict(os.environ, MPV_LOG=str(args.out / "mpv.jsonl"))
            video = run.start("mpv", [str(HERE / "video.sh"), str(clip)], env)
            run.mapped(video)
            run.snapshot("playback")
            time.sleep(args.duration * args.trials)
            if video.poll() is not None:
                raise RuntimeError("mpv exited during playback")
        else:
            for trial in range(args.trials):
                time.sleep(args.idle)
                run.event("trial_begin", trial=trial)
                if args.scenario in ("gtk-launch", "compositor"):
                    title = f"drm-study-{trial}"
                    app = run.start(f"gtk-{trial}", ["zenity", "--text-info", "--filename", str(page),
                                    "--title", title, "--width", "900", "--height", "700"],
                                    dict(os.environ, GDK_BACKEND="wayland"))
                    run.mapped(app, title)
                    run.snapshot(f"trial-{trial}")
                    if args.scenario == "compositor":
                        run.event("workspace_request", trial=trial, destination=workspace + 1)
                        focus(workspace + 1)
                        time.sleep(args.duration)
                        run.event("workspace_request", trial=trial, destination=workspace)
                        focus(workspace)
                    time.sleep(args.duration)
                    run.finish(app)
                else:
                    if browser.poll() is not None:
                        raise RuntimeError("Firefox exited")
                    # Verify focus before sending global input; abort if the user intervened.
                    active = hypr("activewindow")
                    if active.get("title") != "DRM scheduling scroll fixture":
                        raise RuntimeError("Firefox fixture lost focus; refusing to type")
                    run.event("key_request", trial=trial, key="Next" if trial % 2 == 0 else "Prior")
                    subprocess.run(["wtype", "-k", "Next" if trial % 2 == 0 else "Prior"], check=True)
                    run.event("key_return", trial=trial)
                    time.sleep(args.duration)
                    run.snapshot(f"trial-{trial}")
                run.event("trial_end", trial=trial)
        if hog is not None and hog.poll() is not None:
            raise RuntimeError("hog exited before end of run")
        run.event("complete")
    finally:
        try:
            run.close()
        finally:
            focus(original)


if __name__ == "__main__":
    if sys.argv[1:] == ["--cpu-worker"]:
        cpu_worker()
    else:
        main()
