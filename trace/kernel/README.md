# Opt-in gfx admission kernel

`0001-gfx-admission.patch` implements the admission/selection prototype on the
pinned Arch `7.2.11.arch1-1` sources. It is integrated into `PKGBUILD` and has
passed focused KUnit tests and x86 object compilation. **A production package,
boot entry and hardware validation are still pending.** See the
[implementation and validation report](../reports/gfx-admission-implementation.md)
and the [source audit](../reports/amdgpu-submission-audit.md).

The recipe retains Arch's source URLs, signing keys, source/config checksums
and kernel/header packaging. `pkgbase=linux-drmsched` gives the expected release
`7.2.11-arch1-1-drmsched`, its own module directory and `/usr/src/linux-drmsched`
headers link. It does not provide or replace `linux`. Rebuild **the kernel and
all its modules together**: this patch changes internal scheduler struct layouts.
Do not copy either module into the stock kernel's directory.

## Controls and experiment arms

Both selectors are read-only module parameters, initialized with the scheduler.
The module is in the initramfs, so choose them on the test kernel's command line.
`amdgpu.sched_gfx_admission=0` and `amdgpu.sched_gfx_selection=0` are the defaults.

The sole arm registry is [arms.csv](arms.csv). Print its current boot arguments
with `python3 trace/kernel/arms.py`, or supply an arm name to print one line.
Contrast/control roles and primary/secondary scope live in that registry; the
[protocol](../reports/kernel-limit-protocol.md) defines inference and pilot gates.

Admission 1 caps combined NORMAL/LOW **jobs**, including interactive victims,
per gfx scheduler. HIGH/KERNEL remain exempt from this extra cap and still use
original total credits. Selection 1 skips only extra-cap failures, never original
credit failures. Its scan is one tree traversal, O(number of entities), with
examined/skipped counts and elapsed time traced. It has no fixed microsecond
bound. Stock selection can block HIGH behind NORMAL in fair; that arm is a
required diagnostic. Off/skip-cap reduces to stock eligibility.

Admission 2 sets only gfx scheduler credits to one and includes every priority.
All modes retain the global submission clamp, physical ring/fence capacity and
other engines' admission. The scheduler patch leaves the upstream user-queue
default intact. A separate `0002-test-disable-user-queues.patch` makes user queues
**disabled in this test kernel**;
verify `amdgpu.user_queue=0` before capture. Kernel/direct submissions remain
outside the scheduler cap. No sleeper-accounting change is included.

The admission class is the job's immutable priority saved by `drm_sched_job_arm`
(`job->s_priority`), copied into its reservation at dispatch. Changing an entity's
priority does not retag already armed jobs or change their release decision.

## Build handoff

The host still lacks `bc`, `rust`, `rust-bindgen`, and `rust-src`. Installing
build dependencies requires the user's root terminal:

```sh
sudo pacman -S --needed bc rust rust-bindgen rust-src
```

Then run **without sudo**:

```sh
cd /home/jinho/Projects/drm-gpu-scheduling/trace/kernel
makepkg --verifysource
makepkg --nobuild
makepkg --noextract --log
```

Signature verification stays enabled for the production build. Import missing
keys only after checking their fingerprints against the recipe/upstream. The
recipe verifies `audited-files.sha256` after the Arch patch and before the
research patch, applies the scheduler patch with zero fuzz, verifies `scheduler-files.sha256`,
then applies the separate test-default patch and verifies `patched-files.sha256`. Both manifests and the patch are themselves
checksum-pinned sources. A config diff or unexpected kernel release aborts.

The pinned Arch config includes Rust 1.99.0 metadata. Installing a newer
compiler can still cause a config diff. Review and pin any intended toolchain/
config update once, before building the binary for **all** arms. Do not silently
disable Rust just to pass preparation. The x86 compile-only checks used an
isolated config where unavailable Rust options were dropped; they are not an
accepted production config or completed package build.

A successful build packages `drmsched-build.txt` beside the kernel image. It
records the base commit, patch/config/manifest hashes, toolchain versions and
kernel image hashes. `capture.sh` records it, the module artifact hashes and the
running config hash when available.

## Trace validation

The unchanged m3 harness already enables the whole `gpu_scheduler` event group,
which includes the new reservation, dispatch and selection diagnostics. For a
new **bg1** capture:

```sh
python3 /home/jinho/Projects/drm-gpu-scheduling/trace/admission.py /absolute/path/to/run
```

The checker joins background dispatches to reservation IDs, checks priority
exemptions, cap/credit bounds, identities and consecutive per-scheduler transition
serials, and reports scan counts/cost. Empty queues emit no selection event;
selection-event totals therefore count nonempty scans, not every FIFO priority probe. It rejects lost events and missing dispatch
instrumentation. Reports can arrive in a different cross-CPU order; serials
establish reservation order. Normal captures may start/end with a live job; the
output reports those open boundaries. `--require-drained` rejects them for a
controlled drained test. This checker does not validate global1, hardware direct
submissions, fairness, presentation latency or successful reset recovery.

## Reproduce the focused tests

Use a separate clean source tree: unpack the pinned stable archive, apply the
Arch patch, verify `audited-files.sha256`, apply patch 1 and verify
`scheduler-files.sha256`, then apply patch 2 and verify `patched-files.sha256`. Do not generate an in-tree `.config` before the
out-of-tree KUnit build. With `bc` available, set this path to that source tree:

```sh
drmsched_test_src=/absolute/path/to/clean/patched/linux-7.2.11
python3 "$drmsched_test_src/tools/testing/kunit/kunit.py" run \
  --build_dir /home/jinho/Projects/drm-gpu-scheduling/.cache/kunit-admission \
  --kunitconfig /home/jinho/Projects/drm-gpu-scheduling/trace/kernel/kunitconfig \
  --jobs 8 --timeout 90 'drm_sched_admission*'
```

Repeat with `--kernel_args 'gpu_sched.sched_policy=2'` before the filter for fair.
The first invocation uses default FIFO. These UML kernels run as isolated user
processes; they do not boot the host into the test kernel.

## Boot and hardware work remain

Do not install/boot this prototype as though the experiment has passed. The next
handoff must build/review the complete package, inspect its file list and the
actual boot-loader default, prepare a distinct mkinitcpio preset and boot entry,
record stock artifact hashes, and use a one-shot test boot. Never overwrite the
stock UKI, modules or default entry. A newest-entry auto-selection rule needs
inspection before adding an entry.

The local mkinitcpio pacman hook generates a preset for a new `pkgbase`. Its
current template creates conventional kernel/initramfs files, not this machine's
configured UKI. Renaming `pkgbase` does not complete UKI/boot setup.

Before measurement: validate mixed-priority and two-hog invariants in real traces,
error/cancellation/reset progress, three suspend/resume cycles and the 30-minute
soak. Add the solo multi-job and aligned/offset two-victim cells, then run the FIFO feasibility/variance pilot before choosing and freezing the
confirmatory block count. See the [experiment protocol](../reports/kernel-limit-protocol.md).
No installed package, module, preset, UKI, loader entry or stock boot setting was
changed during this implementation.
