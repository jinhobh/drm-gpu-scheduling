# Opt-in kernel experiment base

This is a **build scaffold**, derived from Arch's `7.2.11.arch1-1`
[packaging commit](https://gitlab.archlinux.org/archlinux/packaging/packages/linux/-/tree/0c178d550ec6392265d3a443333229823634bd08).
It does not yet contain the admission patch, a boot entry, or a built package.
The [source audit](../reports/amdgpu-submission-audit.md) is the completed prerequisite.

The recipe retains Arch's source URLs, signature keys, source/config checksums,
kernel config and kernel/header packaging. It changes `pkgbase` to
`linux-drmsched`, removes documentation builds/dependencies and stock kernel
module aliases/replacements, and rejects config drift or an unexpected release.
Expected release: `7.2.11-arch1-1-drmsched`. Modules and the packaged kernel land
under `/usr/lib/modules/7.2.11-arch1-1-drmsched/`; the headers symlink is
`/usr/src/linux-drmsched`. It does not provide or replace `linux`.

## Build handoff

On 2026-11-04, `makepkg --nobuild --noconfirm` stopped before source preparation:
`bc`, `rust`, `rust-bindgen`, and `rust-src` are missing. Installing dependencies
requires the user's root terminal. This command installs only build prerequisites:

```sh
sudo pacman -S --needed bc rust rust-bindgen rust-src
```

After dependencies are installed, run **without sudo**, from the recipe directory:

```sh
cd /home/jinho/Projects/drm-gpu-scheduling/trace/kernel
makepkg --verifysource
makepkg --nobuild
makepkg --noextract --log
```

Signature checking remains enabled. Import missing signing keys after checking
their fingerprints against the recipe and upstream; do not bypass signature
verification for the actual build. A config diff is a build failure requiring
review, rather than permission to silently drop Rust or change drivers. Pin the
accepted toolchain/config for all experiment arms.

Before adding research patches, verify the audited files in the prepared tree:

```sh
cd /home/jinho/Projects/drm-gpu-scheduling/trace/kernel/src/linux-7.2.11
sha256sum -c /home/jinho/Projects/drm-gpu-scheduling/trace/kernel/audited-files.sha256
```

Future admission patches will intentionally change some hashes: preserve the
baseline check before those patches and record both states. Keep the new patch's
selectors off/stock by default and compare all arms on one binary.

## Installation and boot remain a separate step

Do not install this baseline as though it implements the experiment. Finish the
admission patch and compile checks first. A subsequent installation handoff must
inspect the package file list and the actual boot-loader default, create a
distinct mkinitcpio preset and boot entry, record stock artifact hashes, and use
a one-shot test boot. Never overwrite the stock UKI, modules or default entry.
An automatically selected newest entry is not sufficient protection.

The local Arch mkinitcpio pacman hook generates a preset from
`/usr/share/mkinitcpio/hook.preset` for a new `pkgbase`. Its current template
generates conventional kernel/initramfs files, not this machine's configured UKI.
Simply renaming `pkgbase` therefore does **not** complete UKI/boot setup.
No preset, UKI, loader entry, installed module or package was changed here.

## Validation completed

- Recipe passes `bash -n` and `makepkg --printsrcinfo`; split package names are
  `linux-drmsched` and `linux-drmsched-headers`.
- Source archive and Arch patch match both upstream recipe SHA-256 and BLAKE2
  checksums; config matches upstream BLAKE2.
- Arch patch applies to the official archive with `--fuzz=0`; all 14 audited
  source hashes match after application.
- Rootless `olddefconfig` and `kernelrelease` in an ignored audit tree produced
  `7.2.11-arch1-1-drmsched`. Missing Rust silently changed config in that isolated
  check; the recipe now rejects that drift.
- Ran the actual recipe's source preparation in a separate ignored directory
  with dependency/PGP checks bypassed only for this negative test. Archive,
  patch and config checksums passed; preparation stopped at the new config-drift
  guard as expected when Kconfig removed Rust. This does not replace the enabled
  signature/dependency checks required for a real build.

These are source/configuration/recipe checks, **not** a successful kernel/module
compile, package build, boot, recovery test or hardware cap validation.
