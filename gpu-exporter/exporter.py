#!/usr/bin/env python3
"""Minimal Intel Arc (xe driver) metrics exporter for the Glance dashboard.

Exists because nothing off the shelf reads this card. The usual tools assume
NVIDIA (nvidia-smi) or i915 (intel_gpu_top, which fails on Battlemage with
"no discrete/integrated i915 devices found"). nvtop does support it, so this
wraps nvtop and fills the gaps from sysfs.

Serves GET /gpu.json and GET /health.json. No published port; Glance
reaches it over the proxy
network by container name.
"""

import ctypes
import json
import os
import re
import struct
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get('PORT', '8000'))
SYS = os.environ.get('SYS_ROOT', '/sys')


def read(path, default=None):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return default


def read_int(path):
    v = read(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def repair_nvtop_json(raw):
    """nvtop 3.3.1 -s emits invalid JSON: no comma after mem_util, mem_total,
    mem_used. Insert the missing separators rather than hand-parsing, so we
    keep working if upstream ever fixes it."""
    lines = raw.split('\n')
    out = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        nxt = next((l.strip() for l in lines[i + 1:] if l.strip()), '')
        needs_comma = (
            stripped
            and not stripped.endswith((',', '{', '[', ':'))
            and nxt.startswith('"')
        )
        out.append(line + ',' if needs_comma else line)
    return '\n'.join(out)


def nvtop_metrics():
    try:
        raw = subprocess.run(
            ['nvtop', '-s'], capture_output=True, text=True, timeout=10).stdout
    except (subprocess.SubprocessError, OSError) as e:
        return {}, f'nvtop failed: {e}'
    try:
        data = json.loads(repair_nvtop_json(raw))
    except json.JSONDecodeError as e:
        return {}, f'nvtop output unparseable: {e}'
    if not isinstance(data, list) or not data:
        return {}, 'nvtop returned no devices'
    return data[0], None


def num(value):
    """'46C' -> 46.0, '1200MHz' -> 1200.0, '19%' -> 19.0, None -> None."""
    if value is None:
        return None
    m = re.match(r'\s*(-?[\d.]+)', str(value))
    return float(m.group(1)) if m else None


def xe_hwmon():
    """Find the xe hwmon node by name. Numbering is not stable across reboots,
    so hwmon4 must never be hardcoded."""
    base = os.path.join(SYS, 'class/hwmon')
    try:
        nodes = os.listdir(base)
    except OSError:
        return {}
    for node in nodes:
        d = os.path.join(base, node)
        if read(os.path.join(d, 'name')) != 'xe':
            continue
        temps = []
        for f in sorted(os.listdir(d)):
            if re.fullmatch(r'temp\d+_input', f):
                v = read_int(os.path.join(d, f))
                if v:
                    temps.append(v / 1000.0)
        return {
            'temp_c': round(max(temps), 1) if temps else None,
            'power_cap_w': (lambda v: round(v / 1e6, 1) if v else None)(
                read_int(os.path.join(d, 'power1_crit'))),
        }
    return {}


def xe_freq():
    """Actual GT clock from the xe sysfs tree."""
    for root, dirs, files in os.walk(os.path.join(SYS, 'devices')):
        if root.endswith('/freq0') and 'act_freq' in files:
            return {
                'clock_mhz': read_int(os.path.join(root, 'act_freq')),
                'clock_max_mhz': read_int(os.path.join(root, 'max_freq')),
            }
    return {}


# ---------------------------------------------------------------------------
# GPU utilisation, read straight from the xe PMU.
#
# nvtop reports gpu_util as null on this card even with CAP_PERFMON, but the
# counters themselves are readable, so go to the source. Utilisation is the
# ratio of engine-active-ticks to engine-total-ticks across a short interval.
#
# config layout, from the PMU's own format/ directory:
#   event           config:0-11     0x02 active, 0x03 total
#   engine_instance config:12-19
#   engine_class    config:20-27
#   gt              config:60-63
# ---------------------------------------------------------------------------

PMU_ROOT = 'bus/event_source/devices'
NR_PERF_EVENT_OPEN = 298          # x86_64
EV_ACTIVE, EV_TOTAL = 0x02, 0x03

# DRM_XE_ENGINE_CLASS_*. Which of these actually exist, and on which GT, is
# discovered at runtime rather than assumed: on Battlemage the video engines
# live on a separate media tile (gt1) while render/copy/compute sit on gt0, so
# hardcoding gt=0 silently loses exactly the engines a media server cares about.
ENGINE_CLASSES = {
    'render': 0,
    'copy': 1,
    'video_decode': 2,
    'video_enhance': 3,
    'compute': 4,
}
MAX_INSTANCE = 4
MAX_GT = 2

_libc = None


def _libc_handle():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL('libc.so.6', use_errno=True)
    return _libc


def _find_pmu():
    base = os.path.join(SYS, PMU_ROOT)
    try:
        for name in os.listdir(base):
            if name.startswith('xe_'):
                return os.path.join(base, name)
    except OSError:
        pass
    return None


def _perf_open(pmu_type, config):
    attr = struct.pack('=IIQQQQQ', pmu_type, 128, config, 0, 0, 0, 0)
    attr += b'\x00' * (128 - len(attr))
    buf = ctypes.create_string_buffer(attr, 128)
    fd = _libc_handle().syscall(
        NR_PERF_EVENT_OPEN, ctypes.byref(buf), -1, 0, -1, 0)
    return fd if fd >= 0 else None


def _read_counter(fd):
    try:
        return int.from_bytes(os.read(fd, 8), 'little')
    except OSError:
        return None


def pmu_utilisation(sample_s=0.25):
    """Percentage busy per engine class, or {} if the PMU is unreadable."""
    pmu = _find_pmu()
    if not pmu:
        return {}
    try:
        pmu_type = int(read(os.path.join(pmu, 'type')))
    except (TypeError, ValueError):
        return {}

    # Discover which (class, instance, gt) tuples this card actually exposes.
    fds = {}
    for label, cls in ENGINE_CLASSES.items():
        for inst in range(MAX_INSTANCE):
            for gt in range(MAX_GT):
                cfg = (cls << 20) | (inst << 12) | (gt << 60)
                a = _perf_open(pmu_type, cfg | EV_ACTIVE)
                t = _perf_open(pmu_type, cfg | EV_TOTAL)
                if a is None or t is None:
                    for fd in (a, t):
                        if fd is not None:
                            os.close(fd)
                    continue
                fds.setdefault(label, []).append((a, t))

    if not fds:
        return {}

    def snapshot():
        return {k: [(_read_counter(a), _read_counter(t)) for a, t in v]
                for k, v in fds.items()}

    first = snapshot()
    time.sleep(sample_s)
    second = snapshot()

    out = {}
    for label, pairs in first.items():
        # Sum across instances so a class with two engines reads as one figure.
        d_active = d_total = 0
        for (a0, t0), (a1, t1) in zip(pairs, second[label]):
            if None in (a0, t0, a1, t1):
                continue
            d_active += a1 - a0
            d_total += t1 - t0
        if d_total > 0:
            out[label] = round(min(100.0, 100.0 * d_active / d_total), 1)

    for pairs in fds.values():
        for a, t in pairs:
            os.close(a)
            os.close(t)
    return out


def collect():
    nv, err = nvtop_metrics()
    hw = xe_hwmon()
    fq = xe_freq()

    total = num(nv.get('mem_total'))
    used = num(nv.get('mem_used'))

    # Prefer the PMU: nvtop reports gpu_util null on this card even with
    # CAP_PERFMON, but the underlying counters read fine.
    engines = pmu_utilisation()
    util = max(engines.values()) if engines else num(nv.get('gpu_util'))

    return {
        'name': nv.get('device_name') or 'Intel Arc B580',
        # None (not 0) when the PMU is unreadable, so the dashboard can say
        # "unavailable" instead of showing a broken exporter as an idle GPU.
        'gpu_util_pct': util,
        'gpu_util_available': util is not None,
        'engines': engines or None,
        'mem_used_gb': round(used / 1024 ** 3, 2) if used else None,
        'mem_total_gb': round(total / 1024 ** 3, 2) if total else None,
        'mem_util_pct': num(nv.get('mem_util')),
        'temp_c': hw.get('temp_c') if hw.get('temp_c') is not None else num(nv.get('temp')),
        'fan_rpm': num(nv.get('fan_speed')),
        'clock_mhz': fq.get('clock_mhz') if fq.get('clock_mhz') else num(nv.get('gpu_clock')),
        'clock_max_mhz': fq.get('clock_max_mhz'),
        # nvtop 3.3.2 reports live draw; 3.3.1 returned null here.
        'power_draw_w': num(nv.get('power_draw')),
        'power_cap_w': hw.get('power_cap_w'),
        'error': err,
    }


# ---------------------------------------------------------------------------
# Aggregate health, for the dashboard's status ribbon.
#
# Kuma splits what we need across two endpoints -- names live in the status
# payload, state and uptime in the heartbeat payload -- and both are keyed
# maps rather than lists. Reducing that inside a Go template is miserable, so
# it is flattened here instead.
# ---------------------------------------------------------------------------

KUMA = os.environ.get('KUMA_URL', 'http://uptime-kuma:3001')
KUMA_SLUG = os.environ.get('KUMA_SLUG', 'status')


def _kuma(path):
    import urllib.request
    with urllib.request.urlopen(f'{KUMA}{path}', timeout=10) as r:
        return json.load(r)


def health():
    """Aggregate status. Never reports a false all-clear: if Kuma cannot be
    reached the caller gets ok=False and an error, so the ribbon says so
    rather than rendering '32/32 up' from stale assumptions."""
    try:
        status = _kuma(f'/api/status-page/{KUMA_SLUG}')
        beats = _kuma(f'/api/status-page/heartbeat/{KUMA_SLUG}')
    except Exception as e:
        return {'ok': False, 'error': f'kuma unreachable: {e}',
                'total': 0, 'up': 0, 'down': 0, 'down_names': []}

    names = {str(m['id']): m['name']
             for g in status.get('publicGroupList', [])
             for m in g.get('monitorList', [])}

    hb = beats.get('heartbeatList', {}) or {}
    uptimes = beats.get('uptimeList', {}) or {}

    up, down_names, since = 0, [], {}
    for mid, series in hb.items():
        if not series:
            continue
        last = series[-1]
        if last.get('status') == 1:
            up += 1
        else:
            nm = names.get(str(mid), f'monitor {mid}')
            down_names.append(nm)
            # how long it has been failing: walk back while still down
            n = 0
            for beat in reversed(series):
                if beat.get('status') == 1:
                    break
                n += 1
            since[nm] = n

    vals = [v for v in uptimes.values() if isinstance(v, (int, float))]
    overall = round(100.0 * sum(vals) / len(vals), 2) if vals else None

    worst = None
    if uptimes:
        wk = min(uptimes, key=lambda k: uptimes[k])
        worst = {'name': names.get(wk.split('_')[0], wk),
                 'uptime': round(100.0 * uptimes[wk], 2)}

    total = len([s for s in hb.values() if s])
    return {
        'ok': True,
        'total': total,
        'up': up,
        'down': total - up,
        'down_names': sorted(down_names),
        'down_first': sorted(down_names)[0] if down_names else None,
        'down_beats': since.get(sorted(down_names)[0]) if down_names else 0,
        'uptime_24h': overall,
        'worst': worst,
        'error': None,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = self.path.rstrip('/')
        if route in ('/health.json', '/health'):
            try:
                body = json.dumps(health(), indent=2).encode()
            except Exception as e:
                body = json.dumps({'ok': False, 'error': str(e)}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
            return
        if route not in ('/gpu.json', '/gpu', ''):
            self.send_error(404)
            return
        try:
            body = json.dumps(collect(), indent=2).encode()
            code = 200
        except Exception as e:  # never 500 the dashboard
            body = json.dumps({'error': str(e)}).encode()
            code = 200
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass  # quiet; Glance polls frequently


if __name__ == '__main__':
    HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
