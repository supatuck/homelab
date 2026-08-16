#!/usr/bin/env python3
"""Wrapper around nvtop that guarantees a non-empty device_name.

nvtop 3.3.2 reports the Intel Arc B580 correctly in every respect except the
name, which comes back as null. Beszel skips nameless samples outright:

    if sample.DeviceName == "" { continue }      agent/gpu_nvtop.go

so its collector logs `GPU data=map[]` and no GPU ever appears, despite nvtop
itself working. Filling the name in is all that is required.

Beszel invokes `nvtop -lP -d <interval>` (loop mode), not `-s`, so this has to
work on a continuous stream rather than a single document. It therefore does a
line-level substitution on the one field that needs fixing and passes every
other byte through untouched -- no JSON buffering, no added latency, and
nothing else can be corrupted by it.

Installed ahead of the real binary on PATH; interactive use is unaffected.
"""

import os
import re
import subprocess
import sys

REAL = '/usr/local/bin/nvtop'

# "device_name": null   ->   "device_name": "<name>"   (indentation/comma kept)
NULL_NAME = re.compile(r'^(\s*"device_name"\s*:\s*)null(\s*,?\s*)$')


def gpu_name():
    """Human name for the card, from the DRM device's PCI ID."""
    override = os.environ.get('GPU_NAME')
    if override:
        return override
    known = {
        'e20b': 'Intel Arc B580',
        'e20c': 'Intel Arc B570',
        'e211': 'Intel Arc B770',
    }
    try:
        cards = sorted(os.listdir('/sys/class/drm'))
    except OSError:
        return 'GPU'
    for card in cards:
        if not card.startswith('card') or '-' in card:
            continue
        uevent = f'/sys/class/drm/{card}/device/uevent'
        try:
            blob = open(uevent).read()
        except OSError:
            continue
        m = re.search(r'PCI_ID=([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})', blob)
        if not m:
            continue
        vendor, device = m.group(1).lower(), m.group(2).lower()
        if vendor == '8086':
            return known.get(device, f'Intel GPU ({device})')
        return f'GPU {vendor}:{device}'
    return 'GPU'


def main():
    args = sys.argv[1:]

    # Interactive/TUI use has no JSON to rewrite -- hand the terminal over.
    if not any(a.startswith('-') and ('s' in a or 'l' in a) for a in args):
        os.execv(REAL, [REAL] + args)

    name = gpu_name()
    replacement = r'\g<1>"%s"\g<2>' % name

    proc = subprocess.Popen(
        [REAL] + args, stdout=subprocess.PIPE, text=True, bufsize=1)
    try:
        for line in proc.stdout:
            sys.stdout.write(NULL_NAME.sub(replacement, line))
            sys.stdout.flush()          # loop mode is a live stream
    except (BrokenPipeError, KeyboardInterrupt):
        proc.terminate()
        return 0
    finally:
        if proc.stdout:
            proc.stdout.close()
    return proc.wait()


if __name__ == '__main__':
    sys.exit(main())
