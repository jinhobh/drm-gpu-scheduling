# Fair report sharing and delivery handoff

Rechecked 2026-11-04T23:40:25.898977+00:00. User requested upstream submission; **not sent** because no
connected mail-sending service or local sendmail/msmtp/git-send-email transport
is configured. Gmail has been offered but no connection is confirmed. This is
a delivery dependency, not a request for another authorization to submit.

## Upstream recheck

Official branch API and git ls-remote both resolve drm-misc-next to
`e32e83a6c5ec031a7f59d4794113d8484b220254`. Fresh official API source hashes:

- `sched_rq.c`: `634142eb99ad9a0110cb1c2fcd5b3944a3548dd05422f19730c30939242480d6`.
- `sched_entity.c`: `e6183847bb4bb2598fe70934113d2e9a25ec9d1c2e54620f792b0e695ad4ba2a`.

The reviewed successor-only accounting path persists. This is source checking,
not a new hardware reproduction on drm-misc-next. Recheck again if delivery is
delayed. [Official branch API](https://gitlab.freedesktop.org/api/v4/projects/drm%2Fmisc%2Fkernel/repository/branches/drm-misc-next).

Recipient resolution used that tree's MAINTAINERS and get_maintainer.pl with
`--no-tree --nogit --nogit-fallback --no-rolestats -f drivers/gpu/drm/scheduler/sched_rq.c`.
The tool also returns broad DRM/core maintainers; direct scheduler recipients:
To `dri-devel@lists.freedesktop.org`; Cc Matthew Brost, Danilo Krummrich,
Philipp Stanner and Christian König at the addresses in that pinned MAINTAINERS.

## Concrete sharing artifact

- Local archive: `/home/jinho/Projects/drm-gpu-scheduling/.cache/fair-report-share/fair-starvation-20261104.tar.xz`
- Size: 72793884 bytes; SHA-256 `19b9dc9df8f401c3ca8f75394f1e09cc1972837d1bc56d05cfe6c303fb5f8e6c`.
- [Unpublished release draft](https://github.com/jinhobh/drm-gpu-scheduling/releases) (`fair-starvation-20261104`) has the uploaded asset. Its
  download URL is not public until publication; do not send the draft URL as
  if maintainers can already download it.
- Includes 15 original hash-matched inputs, four analysis modules, input manifest,
  compact evidence, README and an archive-wide checksum list. It excludes fdinfo,
  workload logs and unrelated desktop data. Kernel/process metadata and one
  local checkout path are retained; this is not anonymized.
- Extracted independently, every member checksum passed; rerunning its analysis
  reproduced the existing evidence **byte for byte** (21/60 stalls, 5,120 overtakes).
- Rebuild without sending/publishing: `python3 /home/jinho/Projects/drm-gpu-scheduling/trace/share_fair.py --out /absolute/new/archive.tar.xz`.

## Delivery steps

The archive is too large for ordinary mailing-list attachment limits. Once a
sending path is available, publish the reviewed release draft, verify the public
asset URL and checksum, recheck upstream, then send the prepared report with that
link and compact evidence attachment. Do not claim publication or delivery before
those steps succeed. The MIME message is locally prepared at
`/home/jinho/Projects/drm-gpu-scheduling/.cache/fair-report-share/fair-starvation.eml`.
It contains the **future** public asset URL and must not be sent while that URL
is unavailable. Raw data has not been publicly published or emailed by this task.
