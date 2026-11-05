#!/usr/bin/env python3
"""Build an allowlisted, hash-verified fair-report sharing bundle; never publish/send.

Raw trace metadata is preserved, not anonymized. Only the original five input
files per run are included; unrelated fdinfo and desktop logs stay out.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile

REPO = Path(__file__).resolve().parent.parent
FILES = {'trace.dat', 'bpf.txt', 'burst.csv', 'frames.csv', 'meta.txt'}
SOURCES = ['trace/fair_sleepers.py', 'trace/join.py', 'trace/parse.py', 'trace/burst_stats.py',
           'trace/reports/fair-starvation-inputs.json', 'trace/reports/fair-starvation-evidence.txt']


def validated_inputs(repo, manifest):
    result = {}
    for run in manifest['runs']:
        if set(run['files']) != FILES:
            raise ValueError('unexpected raw-file allowlist')
        for name, expected in run['files'].items():
            relative = Path(run['path']) / name
            path = (repo / relative).resolve()
            if relative.is_absolute() or not path.is_relative_to(repo.resolve()):
                raise ValueError('input escapes repository')
            data = path.read_bytes()
            if len(data) != expected['bytes'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
                raise ValueError(f'input hash/size changed: {relative}')
            if relative.as_posix() in result:
                raise ValueError('duplicate input')
            result[relative.as_posix()] = data
    if not result:
        raise ValueError('empty manifest')
    return result


def build(out):
    manifest = json.loads((REPO / 'trace/reports/fair-starvation-inputs.json').read_text())
    entries = validated_inputs(REPO, manifest)
    for name in SOURCES:
        entries[name] = (REPO / name).read_bytes()
    entries['README.txt'] = b'''Existing m3-p2 fair-starvation capture evidence, 2026-10-19.
No new drm-misc-next hardware reproduction or tested fix is claimed.
Raw trace.dat, bpf.txt, burst.csv, frames.csv and meta.txt are byte-for-byte
originals checked against trace/reports/fair-starvation-inputs.json.
Privacy review: metadata includes task/thread names, PIDs, GPU identifiers,
timing, a local checkout path, and kernel trace metadata/addresses. Not anonymized.
Unrelated clients.jsonl, desktop logs, GPU metrics and environment are omitted.
Requires Python 3 and trace-cmd. No capture/root commands are run by analysis.
From this extracted directory:
  sha256sum -c MANIFEST.sha256
  python3 trace/fair_sleepers.py traces/m3-p2/auto-vkmark-cube-r*/*/
Compare output to trace/reports/fair-starvation-evidence.txt (21/60 waits,
5120 overtaking empty pops). Paths in the input manifest refer to this layout.
'''
    entries['MANIFEST.sha256'] = ''.join(hashlib.sha256(data).hexdigest() + '  ' + name + '\n'
                                        for name, data in sorted(entries.items())).encode()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('xb') as stream, tarfile.open(fileobj=stream, mode='w:xz', preset=6) as archive:
        for name, data in sorted(entries.items()):
            info = tarfile.TarInfo('fair-starvation/' + name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            archive.addfile(info, io.BytesIO(data))
    print(f'{hashlib.sha256(out.read_bytes()).hexdigest()}  {out} ({out.stat().st_size} bytes)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    build(args.out)
