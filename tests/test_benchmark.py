import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
import hf_network_benchmark as bench


class BenchmarkTests(unittest.TestCase):
    def test_paths_and_file_size_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'config.json').write_bytes(b'abc')
            bench.verify_sizes(root, {'config.json': 3})
            for files in [{'config.json': 4}, {'missing': 1}]:
                with self.assertRaises(ValueError):
                    bench.verify_sizes(root, files)
            for name in ['../escape', '/absolute', 'folder/../../escape', 'folder\\escape']:
                with self.assertRaises(ValueError):
                    bench.safe_file_path(root, name)

    def test_selected_files_are_exact_and_require_sizes(self):
        files = [SimpleNamespace(rfilename='config.json', size=3),
                 SimpleNamespace(rfilename='weights.safetensors', size=8)]
        self.assertEqual(bench.select_files(files, ['*.json']), {'config.json': 3})
        with self.assertRaises(ValueError):
            bench.select_files(files, ['nothing'])
        with self.assertRaises(ValueError):
            bench.select_files([SimpleNamespace(rfilename='x', size=None)], [])

    def transfer(self, response, limit=8):
        real_client = httpx.Client
        transport = httpx.MockTransport(lambda request: response)
        with patch('httpx.Client', side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)), \
             patch.object(bench, 'route_to', return_value={'host': 'fixture'}), \
             patch.object(bench, 'counters', return_value={}):
            return bench.http_transfer('https://fixture.invalid/model', limit, 5)

    def test_http_range_and_short_response(self):
        good = httpx.Response(206, headers={'Content-Range': 'bytes 0-7/100'}, stream=httpx.ByteStream(b'12345678'))
        result = self.transfer(good)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['bytes_read'], 8)
        self.assertEqual(result['transfer_host'], 'fixture.invalid')
        short = self.transfer(httpx.Response(206, headers={'Content-Range': 'bytes 0-3/100'}, stream=httpx.ByteStream(b'1234')))
        self.assertEqual(short['status'], 'short_response')
        self.assertEqual(short['bytes_read'], 4)

    def test_http_rejects_encoding_and_wrong_range(self):
        for headers in [{'Content-Encoding': 'gzip'}, {'Content-Range': 'bytes 8-15/100'}]:
            with self.assertRaises(ValueError):
                self.transfer(httpx.Response(206, headers=headers, stream=httpx.ByteStream(b'12345678')))

    def test_worker_kill_is_failed_without_fabricated_speed(self):
        worker = SimpleNamespace(wait=lambda timeout: -9)
        with tempfile.TemporaryDirectory() as temp, patch.object(bench.subprocess, 'Popen', return_value=worker):
            result = bench.supervised_download(Path(temp), {'model': 'test/model'}, 1)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['worker_returncode'], -9)
        self.assertNotIn('average_MB_per_second', result)

    def test_compare_flags_different_workloads_and_ignores_failed_rate(self):
        reports = []
        for label, revision, status, rate in [('baseline', 'a', 'complete', 100),
                                               ('faster', 'b', 'complete', 200),
                                               ('failed', 'a', 'failed', 99999)]:
            reports.append({'schema_version': 1, 'label': label, 'hostname': label,
                            'status': status, 'model': {'repo_id': 'test/model', 'revision': revision,
                                                       'selected_files': {'x': 10}},
                            'download': {'status': status, 'average_MB_per_second': rate, 'elapsed_seconds': 1}})
        with tempfile.TemporaryDirectory() as temp:
            paths = []
            for index, report in enumerate(reports):
                p = Path(temp) / f'{index}.json'
                bench.write_json(p, report)
                paths.append(p)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                bench.compare(SimpleNamespace(reports=paths, output=None))
        value = output.getvalue()
        self.assertIn('2.00×', value)
        self.assertIn('not an equivalent-workload comparison', value)
        self.assertNotIn('99999', value)

    def test_memory_limits_include_ancestor_user_limit(self):
        data = {'/proc/self/cgroup': '0::/user.slice/user-123.slice/session.scope',
                '/sys/fs/cgroup/user.slice/user-123.slice/memory.max': '5368709120'}
        with patch.object(bench, 'read_text', side_effect=lambda path: data.get(str(path))):
            limits = bench.memory_limits()
        self.assertEqual(limits[0]['memory.max'], '5368709120')

    def test_proxy_credentials_are_not_reported(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://user:secret@proxy.invalid:8080/path?token=secret'}, clear=True):
            endpoints = bench.proxy_endpoints()
        self.assertEqual(endpoints['HTTPS_PROXY']['host'], 'proxy.invalid')
        self.assertEqual(endpoints['HTTPS_PROXY']['port'], 8080)
        self.assertNotIn('secret', json.dumps(endpoints))


if __name__ == '__main__':
    unittest.main()
