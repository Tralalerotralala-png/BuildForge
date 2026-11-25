"""Generate reviewable retry plans from local JSONL session logs. Never stops jobs."""
import argparse
import json
import statistics
from pathlib import Path


def _merge_active(dst, name, row):
    prev = dst.get(name)
    if prev is None:
        dst[name] = dict(row)
        return
    merged = dict(row)
    prev_sec = prev.get('seconds')
    cur_sec = row.get('seconds')
    if isinstance(prev_sec, (int, float)) and isinstance(cur_sec, (int, float)):
        merged['seconds'] = max(prev_sec, cur_sec)
    elif isinstance(prev_sec, (int, float)) and not isinstance(cur_sec, (int, float)):
        merged['seconds'] = prev_sec
    dst[name] = merged


def analyze(lines, threshold=900, base_opt='1', expected_names=None):
    if threshold < 1 or str(base_opt) not in ('0', '1', '2'):
        raise ValueError('Invalid threshold or optimization')
    latest = {}
    seen_active = {}
    inferred_peak = {}
    completed = {}
    last = {}
    done = {}
    malformed = 0
    tail = None
    tail_start = None
    expected = set(expected_names) if expected_names is not None else None

    for line in lines:
        try:
            e = json.loads(line)
        except (ValueError, TypeError):
            malformed += 1
            continue
        if not isinstance(e, dict):
            malformed += 1
            continue
        if e.get('event') == 'phase' and str(e.get('message', '')).startswith('Checking toolchain'):
            latest.clear()
            seen_active.clear()
            inferred_peak.clear()
            completed.clear()
            done.clear()
            last = {}
            tail = None
            tail_start = None
        if e.get('event') == 'progress':
            last = e
            active = e.get('active_units', [])
            latest = (
                {x['name']: x for x in active if isinstance(x, dict) and isinstance(x.get('name'), str)}
                if isinstance(active, list)
                else {}
            )
            for name, row in latest.items():
                _merge_active(seen_active, name, row)
            # Older logs did not list the active worker names. Infer only when
            # the exact caller-provided source set and every terminal count agree.
            count = e.get('done')
            total = e.get('total')
            seconds = e.get('seconds')
            running = e.get('active')
            valid = (
                expected is not None
                and not latest
                and e.get('queued') == 0
                and type(count) is int
                and type(total) is int
                and type(running) is int
                and isinstance(seconds, (int, float))
                and seconds >= 0
                and total == len(expected)
                and count == len(done)
                and set(done) <= expected
                and running > 0
                and running == total - count
            )
            missing = frozenset(expected - set(done)) if valid else None
            if missing != tail or (tail_start is not None and seconds < tail_start):
                tail = missing
                tail_start = seconds if valid else None
            if valid and tail_start is None:
                tail_start = seconds
            if valid and tail and tail_start is not None:
                elapsed_tail = seconds - tail_start
                for name in tail:
                    if elapsed_tail > inferred_peak.get(name, -1):
                        inferred_peak[name] = elapsed_tail

        elif e.get('event') == 'unit' and isinstance(e.get('name'), str):
            name = e['name']
            state = e.get('state')
            done[name] = state
            latest.pop(name, None)
            if state in ('compiled', 'cached'):
                seen_active.pop(name, None)
                inferred_peak.pop(name, None)
            if state == 'compiled' and isinstance(e.get('seconds'), (int, float)):
                completed[name] = e['seconds']

    candidates_map = {}
    for name, row in seen_active.items():
        if done.get(name) not in ('compiled', 'cached'):
            candidates_map[name] = row
    for name, row in latest.items():
        if done.get(name) not in ('compiled', 'cached'):
            _merge_active(candidates_map, name, row)
    for name, elapsed_tail in inferred_peak.items():
        if name not in candidates_map and done.get(name) not in ('compiled', 'cached'):
            candidates_map[name] = {'name': name, 'seconds': elapsed_tail, 'inferred': True}

    median = statistics.median(completed.values()) if completed else None
    rows = []
    for name, row in candidates_map.items():
        if done.get(name) in ('compiled', 'cached'):
            continue
        elapsed = row.get('seconds')
        cpu = row.get('cpu_seconds')
        if not isinstance(elapsed, (int, float)) or elapsed < threshold:
            continue
        if Path(name).name != name or '/' in name or '\\' in name or '|' in name or '\n' in name or '\r' in name:
            continue
        if Path(name).suffix.lower() not in ('.cpp', '.cc', '.cxx', '.c'):
            continue
        rows.append(
            {
                'name': name,
                'elapsed_seconds': elapsed,
                'cpu_seconds': cpu,
                'rss_mib': row.get('rss_mib'),
                'relative_to_median': round(elapsed / median, 2) if median and median > 0 else None,
                'suggested_opt': '0' if str(base_opt) != '0' else None,
                'timing': True,
                'failed': done.get(name) == 'failed',
                'evidence': (
                    'exhausted queue, exact source-set inference; elapsed lower bound'
                    if row.get('inferred')
                    else 'named active worker sample'
                ),
            }
        )
    rows.sort(key=lambda x: (-x['elapsed_seconds'], x['name']))
    return {
        'schema': 1,
        'threshold_seconds': threshold,
        'completed_median_seconds': median,
        'last_progress': {k: last.get(k) for k in ('done', 'total', 'active', 'queued', 'failed')},
        'candidates': rows,
        'malformed_lines': malformed,
        'note': (
            'Elapsed time alone is not evidence of a stall. Review suggestions; stop the previous build safely '
            'before applying. O0 may affect performance or correctness.'
        ),
    }


def target_lines(report):
    return '\n'.join(
        f"{r['name']} | {r['suggested_opt'] if r['suggested_opt'] is not None else 'same'} | timing"
        for r in report['candidates']
    )


def expected_from_folder(folder, recursive=False):
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError('Source folder not found')
    files = [
        p
        for ext in ('*.cpp', '*.cc', '*.cxx', '*.c')
        for p in (folder.rglob(ext) if recursive else folder.glob(ext))
        if p.is_file()
    ]
    if len({p.stem.casefold() for p in files}) != len(files):
        raise ValueError('Duplicate source basenames; select a narrower folder')
    return [p.name for p in files]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('session_log', type=Path)
    p.add_argument('--threshold', type=int, default=900)
    p.add_argument('--base-opt', choices=['0', '1', '2'], default='1')
    p.add_argument('--targets', action='store_true')
    p.add_argument('--source-dir', type=Path)
    p.add_argument('--recursive', action='store_true')
    a = p.parse_args()
    with a.session_log.open(encoding='utf-8', errors='replace') as f:
        r = analyze(
            f,
            a.threshold,
            a.base_opt,
            expected_from_folder(a.source_dir, a.recursive) if a.source_dir else None,
        )
    print(target_lines(r) if a.targets else json.dumps(r, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
