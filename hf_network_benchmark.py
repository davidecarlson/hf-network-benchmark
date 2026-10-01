#!/usr/bin/env python3
"""Fresh Hugging Face downloads, host network inspection, and cross-host comparison."""

import argparse
from datetime import datetime, timezone
import fnmatch
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import uuid

DEFAULT_MODEL = 'Qwen/Qwen3.8-27B'
COUNTER_NOTE = 'Host-wide counters include unrelated traffic and can lag; not per-process wire accounting.'


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.replace(path)


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def command(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return {'command': argv, 'returncode': result.returncode,
                'stdout': result.stdout.strip(), 'stderr': result.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'command': argv, 'error_type': type(exc).__name__}


def counters():
    values = {}
    for dev in Path('/sys/class/net').glob('*'):
        rx = read_text(dev / 'statistics/rx_bytes')
        tx = read_text(dev / 'statistics/tx_bytes')
        if rx is not None and tx is not None:
            values[dev.name] = {'rx': int(rx), 'tx': int(tx)}
    return values


def counter_delta(before, after):
    return {name: {key: after[name][key] - values[key] for key in ['rx', 'tx']}
            for name, values in before.items() if name in after}


def route_to(host):
    result = {'host': host, 'addresses': [], 'routes': []}
    try:
        addresses = sorted({row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
        result['addresses'] = addresses
        for address in addresses:
            flag = '-6' if ipaddress.ip_address(address).version == 6 else '-4'
            result['routes'].append(command(['ip', flag, 'route', 'get', address]))
    except OSError as exc:
        result['error_type'] = type(exc).__name__
    return result


def proxy_endpoints():
    """Record configured proxy hosts without credentials or signed URL queries."""
    proxies = {}
    for name in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']:
        value = os.environ.get(name)
        if value:
            parsed = urllib.parse.urlsplit(value if '://' in value else 'http://' + value)
            try:
                proxies[name] = {'scheme': parsed.scheme, 'host': parsed.hostname, 'port': parsed.port}
            except ValueError:
                proxies[name] = {'error_type': 'InvalidProxyURL'}
    return proxies


def memory_limits():
    """Record visible cgroup v2 limits, including ancestor user limits."""
    group = read_text('/proc/self/cgroup')
    results = []
    if group:
        for line in group.splitlines():
            if not line.startswith('0::'):
                continue
            relative = PurePosixPath(line[3:].lstrip('/'))
            base = Path('/sys/fs/cgroup') / str(relative)
            while base != Path('/sys/fs'):
                fields = {name: read_text(base / name) for name in
                          ['memory.max', 'memory.current', 'memory.peak', 'memory.events']}
                if any(value is not None for value in fields.values()):
                    results.append({'path': str(base), **fields})
                if base == Path('/sys/fs/cgroup'):
                    break
                base = base.parent
    return results


def inspect_network(host='huggingface.co', include_ethtool=True):
    interfaces = {}
    for dev in sorted(Path('/sys/class/net').glob('*')):
        info = {field: read_text(dev / field) for field in ['operstate', 'speed', 'duplex', 'mtu']}
        speed = info.pop('speed')
        info['reported_speed_Mbps'] = int(speed) if speed and speed.lstrip('-').isdigit() and int(speed) > 0 else None
        uevent = read_text(dev / 'device/uevent') or ''
        info['driver'] = next((line.split('=', 1)[1] for line in uevent.splitlines()
                               if line.startswith('DRIVER=')), None)
        if include_ethtool and info['operstate'] == 'up' and dev.name != 'lo':
            info['ethtool'] = command(['ethtool', dev.name])
        interfaces[dev.name] = info
    proxies = proxy_endpoints()
    return {'interfaces': interfaces, 'ipv4_routes': command(['ip', '-4', 'route']),
            'ipv6_routes': command(['ip', '-6', 'route']),
            'addresses': command(['ip', '-brief', 'address']),
            'destination': route_to(host), 'memory_limits': memory_limits(),
            'configured_proxy_endpoints': proxies,
            'proxy_routes': [route_to(name) for name in sorted({p['host'] for p in proxies.values() if p.get('host')})],
            'proxy_note': 'Proxy settings and NO_PROXY rules may alter the actual HTTP path; destination lookups alone do not prove proxy use.'}


def base_report(label):
    versions = {}
    for package in ['huggingface-hub', 'hf-xet']:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return {'schema_version': 1, 'created_at_utc': now(), 'label': label,
            'hostname': socket.gethostname(), 'platform': platform.platform(),
            'python_version': platform.python_version(), 'package_versions': versions,
            'counter_caveat': COUNTER_NOTE}


def setup_cache(cache, args):
    for key, subdir in [('HF_HOME', ''), ('HF_HUB_CACHE', 'hub'),
                        ('HF_XET_CACHE', 'xet'), ('HF_ASSETS_CACHE', 'assets')]:
        os.environ[key] = str(cache / subdir)
    os.environ['HF_XET_NUM_CONCURRENT_RANGE_GETS'] = str(args.range_gets)
    os.environ['HF_XET_HIGH_PERFORMANCE'] = '1' if args.high_performance else '0'
    os.environ['HF_HUB_DISABLE_XET'] = '0'
    os.environ['HF_XET_CHUNK_CACHE_SIZE_BYTES'] = '0'
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['HF_DEBUG'] = '0'
    # Token values are never written to reports. HF_TOKEN is still honored.


def safe_file_path(directory, name):
    relative = PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts or '\\' in name:
        raise ValueError('Unsafe repository file path')
    return directory / str(relative)


def select_files(siblings, includes):
    result = {}
    for file in siblings:
        if includes and not any(fnmatch.fnmatch(file.rfilename, pattern) for pattern in includes):
            continue
        safe_file_path(Path('.'), file.rfilename)
        if file.size is None:
            raise ValueError('Repository metadata omitted a file size')
        result[file.rfilename] = file.size
    if not result:
        raise ValueError('No repository files matched')
    return result


def verify_sizes(directory, files):
    bad = []
    for name, expected in files.items():
        path = safe_file_path(directory, name)
        if not path.is_file() or path.stat().st_size != expected:
            bad.append(name)
    if bad:
        raise ValueError(f'File size mismatch for {len(bad)} files')


def download_worker(config_path):
    """Separate process lets the supervisor report a worker killed by OOM."""
    config = json.loads(Path(config_path).read_text())
    result_path = Path(config['result_path'])
    try:
        from huggingface_hub import snapshot_download
        before = counters()
        started_at = now()
        start = time.perf_counter()
        snapshot_download(repo_id=config['model'], revision=config['revision'],
                          local_dir=config['model_dir'], max_workers=config['workers'],
                          allow_patterns=config['includes'] or None)
        elapsed = time.perf_counter() - start
        finished_at = now()
        after = counters()
        verify_sizes(Path(config['model_dir']), config['files'])
        total = sum(config['files'].values())
        result = {'status': 'complete', 'started_at_utc': started_at,
                  'finished_at_utc': finished_at, 'elapsed_seconds': elapsed,
                  'total_bytes': total, 'files': len(config['files']),
                  'average_MB_per_second': total / elapsed / 1e6,
                  'average_MiB_per_second': total / elapsed / 2**20,
                  'verified_file_sizes': True,
                  'interface_byte_deltas': counter_delta(before, after),
                  'speed_basis': 'Completed file bytes / snapshot_download wall-clock seconds; excludes startup and verification.'}
        write_json(result_path, result)
        return 0
    except Exception as exc:
        # Exceptions from clients may contain signed URLs; do not persist their text.
        write_json(result_path, {'status': 'failed', 'error_type': type(exc).__name__})
        print(f'Download failed: {type(exc).__name__}', file=sys.stderr)
        return 1


def supervised_download(run_dir, config, interval):
    config_path = run_dir / 'download-job.json'
    config['result_path'] = str(run_dir / 'download-result.json')
    write_json(config_path, config)
    started = time.perf_counter()
    worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_worker', str(config_path)])
    try:
        while True:
            try:
                code = worker.wait(timeout=interval)
                break
            except subprocess.TimeoutExpired:
                print(f'Download running: {time.perf_counter()-started:.0f}s elapsed', flush=True)
    except BaseException:
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()
        raise
    result_path = Path(config['result_path'])
    if result_path.exists():
        result = json.loads(result_path.read_text())
    else:
        result = {'status': 'failed', 'error_type': 'WorkerTerminated',
                  'note': 'No completion metrics were produced. A negative return code indicates a signal; SIGKILL may be OOM or an external kill.'}
    result['worker_returncode'] = code
    result['supervised_wall_seconds'] = time.perf_counter() - started
    if code != 0:
        result['status'] = 'failed'
    return result


def http_transfer(url, limit, timeout, token=None):
    """Discard an identity-encoded HTTP range response with bounded memory."""
    import httpx
    before = counters()
    start = time.perf_counter()
    headers = {'Range': f'bytes=0-{limit-1}', 'Accept-Encoding': 'identity'}
    # httpx strips Authorization on redirects to another origin.
    if token:
        headers['Authorization'] = f'Bearer {token}'
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        with client.stream('GET', url, headers=headers) as response:
            response.raise_for_status()
            if response.headers.get('content-encoding', 'identity').lower() not in ['identity', '']:
                raise ValueError('Server returned an encoded response')
            if response.status_code == 206:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+|\*)', response.headers.get('content-range', ''))
                if not match or int(match[1]) != 0:
                    raise ValueError('Unexpected HTTP range')
            count = 0
            payload_start = time.perf_counter()
            for block in response.iter_raw(chunk_size=1024**2):
                count += min(len(block), limit - count)
                if count >= limit:
                    break
            payload_seconds = time.perf_counter() - payload_start
            elapsed = time.perf_counter() - start
            host = urllib.parse.urlsplit(str(response.url)).hostname
            response_status = response.status_code
            content_range = response.headers.get('content-range')
    after = counters()
    return {'status': 'complete' if count == limit else 'short_response',
            'requested_bytes': limit, 'bytes_read': count, 'elapsed_seconds': elapsed,
            'payload_seconds': payload_seconds, 'average_MB_per_second': count / elapsed / 1e6,
            'payload_MB_per_second': count / payload_seconds / 1e6 if payload_seconds else None,
            'http_status': response_status, 'content_range': content_range,
            'transfer_host': host, 'transfer_routes': route_to(host),
            'interface_byte_deltas': counter_delta(before, after),
            'speed_basis': 'Uncompressed application bytes / HTTP elapsed seconds; data discarded.'}


def report_markdown(report):
    lines = ['# Hugging Face network benchmark', '',
             f"- Label: `{report['label']}`", f"- Host: `{report['hostname']}`",
             f"- Status: `{report['status']}`", '']
    if 'model' in report:
        lines += [f"- Model: `{report['model']['repo_id']}`",
                  f"- Revision: `{report['model']['revision']}`", '']
    lines += ['| Test | Seconds | MB/s |', '|---|---:|---:|']
    for key, title in [('download', 'Fresh snapshot'), ('http_test', 'HTTP transfer')]:
        value = report.get(key, {})
        if value.get('status') == 'complete':
            lines.append(f"| {title} | {value['elapsed_seconds']:.2f} | {value['average_MB_per_second']:.2f} |")
        elif value:
            lines.append(f"| {title}: {value.get('status')} | — | — |")
    lines += ['', 'Snapshot throughput counts completed file bytes, which may exceed wire bytes with Xet compression.',
              COUNTER_NOTE, '', 'See `report.json` for routes, settings, versions, and error types.', '']
    return '\n'.join(lines)


def save_report(run_dir, report):
    write_json(run_dir / 'report.json', report)
    (run_dir / 'report.md').write_text(report_markdown(report))


def run(args):
    label = args.label or socket.gethostname()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    run_dir = args.output.expanduser().resolve() / f'{stamp}-{uuid.uuid4().hex[:8]}'
    run_dir.mkdir(parents=True, exist_ok=False)
    cache = run_dir / 'cache'
    cache.mkdir()
    model_dir = args.model_dir.expanduser().resolve() if args.model_dir else run_dir / 'model'
    report = base_report(label)
    report.update(status='running', run_directory=str(run_dir),
                  network=inspect_network(),
                  settings={'workers': args.workers, 'range_gets': args.range_gets,
                            'high_performance': args.high_performance, 'includes': args.include,
                            'fresh_cache': True, 'fresh_destination': True,
                            'http_test_mib': args.http_test_mib, 'skip_download': args.skip_download})
    save_report(run_dir, report)
    print(f'Report directory: {run_dir}', flush=True)
    try:
        if not args.skip_download and model_dir.exists():
            raise FileExistsError('Choose a new model directory')
        setup_cache(cache, args)
        from huggingface_hub import HfApi, get_token, hf_hub_url
        info = HfApi().model_info(args.model, revision=args.revision, files_metadata=True)
        files = select_files(info.siblings, args.include)
        report['model'] = {'repo_id': args.model, 'revision': info.sha,
                           'selected_files': files, 'total_bytes': sum(files.values()),
                           'model_directory': str(model_dir)}
        save_report(run_dir, report)
        if not args.skip_download:
            model_dir.mkdir(parents=True, exist_ok=False)
            free = shutil.disk_usage(model_dir).free
            if free < sum(files.values()):
                raise OSError('Insufficient free filesystem space for selected files')
            print(f"Fresh download: {sum(files.values())/1e9:.2f} GB; revision {info.sha}", flush=True)
            report['download'] = supervised_download(run_dir,
                {'model': args.model, 'revision': info.sha, 'files': files,
                 'model_dir': str(model_dir), 'workers': args.workers, 'includes': args.include},
                args.progress_interval)
            report['network']['memory_limits_after_download'] = memory_limits()
            save_report(run_dir, report)
        if args.http_test_mib:
            limit = args.http_test_mib * 2**20
            candidates = [(name, size) for name, size in files.items() if size >= limit]
            if not candidates:
                report['http_test'] = {'status': 'skipped', 'reason': 'No selected file is large enough; lower --http-test-mib.'}
            else:
                # Prefer a model weight file, then use the largest selected file.
                name, size = max(candidates, key=lambda item: (item[0].endswith(('.safetensors', '.bin')), item[1]))
                print(f'HTTP range test: {args.http_test_mib} MiB from {name}', flush=True)
                try:
                    report['http_test'] = http_transfer(hf_hub_url(args.model, filename=name, revision=info.sha),
                                                       limit, args.timeout, get_token())
                    report['http_test']['filename'] = name
                except Exception as exc:
                    report['http_test'] = {'status': 'failed', 'error_type': type(exc).__name__}
        failed = any(report.get(key, {}).get('status') in ['failed', 'short_response'] for key in ['download', 'http_test'])
        report['status'] = 'failed' if failed else 'complete'
    except KeyboardInterrupt:
        report.update(status='interrupted', error_type='KeyboardInterrupt')
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__)
        print(f'Benchmark failed: {type(exc).__name__}. Check dependencies, network access, credentials, and free space.', file=sys.stderr)
    report['finished_at_utc'] = now()
    save_report(run_dir, report)
    print(report_markdown(report), flush=True)
    return 0 if report['status'] == 'complete' else 1


def compare(args):
    reports = [json.loads(path.read_text()) for path in args.reports]
    baseline = reports[0].get('download', {}).get('average_MB_per_second')
    if reports[0].get('download', {}).get('status') != 'complete':
        baseline = None
    http_baseline = reports[0].get('http_test', {}).get('average_MB_per_second')
    if reports[0].get('http_test', {}).get('status') != 'complete':
        http_baseline = None
    lines = ['# Host download comparison', '',
             '| Label | Host | Status | Snapshot MB/s | HTTP MB/s | Snapshot seconds | Snapshot speedup | HTTP speedup |',
             '|---|---|---|---:|---:|---:|---:|---:|']
    model_keys = set()
    tuning_keys = set()
    version_keys = set()
    for report in reports:
        if report.get('schema_version') != 1:
            raise ValueError('Unsupported report schema')
        dl = report.get('download', {})
        ht = report.get('http_test', {})
        complete = dl.get('status') == 'complete'
        rate = dl.get('average_MB_per_second') if complete else None
        http_rate = ht.get('average_MB_per_second') if ht.get('status') == 'complete' else None
        seconds = dl.get('elapsed_seconds') if complete else None
        fmt = lambda value: f'{value:.2f}' if value is not None else '—'
        label = report['label'].replace('|', '\\|').replace('\n', ' ')
        host = report['hostname'].replace('|', '\\|').replace('\n', ' ')
        lines.append(f"| {label} | {host} | {report['status']} | {fmt(rate)} | {fmt(http_rate)} | {fmt(seconds)} | {fmt(rate/baseline) + '×' if rate is not None and baseline else '—'} | {fmt(http_rate/http_baseline) + '×' if http_rate is not None and http_baseline else '—'} |")
        if complete:
            model = report.get('model', {})
            model_keys.add((model.get('repo_id'), model.get('revision'),
                            tuple(sorted(model.get('selected_files', {}).items()))))
            settings = report.get('settings', {})
            tuning_keys.add(tuple(settings.get(key) for key in ['workers', 'range_gets', 'high_performance']))
            version_keys.add(tuple(sorted(report.get('package_versions', {}).items())))
    lines += ['', 'Snapshot MB/s measures completed file bytes, not wire bytes. HTTP MB/s measures an identity-encoded transfer.',
              'Compare the same revision, selected files, settings, and software versions. Run order and shared-node load can affect results.', '']
    if len(model_keys) > 1:
        lines += ['**Models, revisions, or selected files differ; this is not an equivalent-workload comparison.**', '']
    if len(tuning_keys) > 1:
        lines += ['**Download settings differ between completed runs.**', '']
    if len(version_keys) > 1:
        lines += ['**Software versions differ between completed runs.**', '']
    output = '\n'.join(lines)
    print(output)
    if args.output:
        args.output.write_text(output)
    return 0


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('Must be greater than zero')
    return number


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError('Must be zero or greater')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    inspect = sub.add_parser('inspect', help='Inspect local interfaces and routes without downloading')
    inspect.add_argument('--host', default='huggingface.co')
    inspect.add_argument('--output', type=Path)
    inspect.add_argument('--no-ethtool', action='store_true')
    benchmark = sub.add_parser('run', help='Fresh model download plus an HTTP range test')
    benchmark.add_argument('--model', default=DEFAULT_MODEL)
    benchmark.add_argument('--revision', help='Commit/tag/branch; use the same commit on every host')
    benchmark.add_argument('--label', help='For example cluster-a-login or cluster-a-compute')
    benchmark.add_argument('--output', type=Path, default=Path('benchmark-results'))
    benchmark.add_argument('--model-dir', type=Path, help='Must not already exist')
    benchmark.add_argument('--workers', type=positive, default=2)
    benchmark.add_argument('--range-gets', type=positive, default=4)
    benchmark.add_argument('--high-performance', action='store_true')
    benchmark.add_argument('--include', action='append', default=[], help='Optional repository glob; repeat for multiple patterns')
    benchmark.add_argument('--http-test-mib', type=nonnegative, default=1024, help='Uncompressed transfer size; 0 disables it')
    benchmark.add_argument('--skip-download', action='store_true', help='Inspect and HTTP-test only; do not download the full model')
    benchmark.add_argument('--timeout', type=positive, default=60)
    benchmark.add_argument('--progress-interval', type=positive, default=20)
    comparison = sub.add_parser('compare', help='Compare saved report.json files; first is the baseline')
    comparison.add_argument('reports', nargs='+', type=Path)
    comparison.add_argument('--output', type=Path)
    worker = sub.add_parser('_worker', help='Internal download worker; invoked automatically')
    worker.add_argument('config_path')
    args = parser.parse_args(argv)
    if args.action == '_worker':
        return download_worker(args.config_path)
    if args.action == 'inspect':
        result = base_report(socket.gethostname())
        result['network'] = inspect_network(args.host, not args.no_ethtool)
        if args.output:
            write_json(args.output, result)
        print(json.dumps(result, indent=2))
        return 0
    if args.action == 'run':
        return run(args)
    return compare(args)


if __name__ == '__main__':
    sys.exit(main())
