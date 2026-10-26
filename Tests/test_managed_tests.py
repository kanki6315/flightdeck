import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import flightdeck as f
import test_adapters
import test_engine


class ManagedTests(unittest.TestCase):
    setUp = test_engine.EngineTests.setUp
    tearDown = test_engine.EngineTests.tearDown
    job = test_engine.EngineTests.job
    # Inherit the isolated state fixture; these assertions exercise real workers and leases.
    def args(self, **kwargs):
        values = dict(project='test', worktree=None, mode='test', skip_tests=False, build=None, device=None)
        values.update(kwargs)
        return argparse.Namespace(**values)

    def launch(self, p=None, device='phone'):
        p = p or self.p
        script = Path(p['root']) / '.flightdeck/test.sh'
        script.parent.mkdir(exist_ok=True)
        script.write_text('#!/bin/bash\n# FLIGHTDECK_TEST_PROTOCOL=1\necho test-output\n')
        inventory = [dict(id=device, family='iPhone', lease=None, state='Booted', name='Phone')]
        with patch.object(f, 'project', return_value=p), patch.object(f, 'check', return_value={'ready': True}), patch.object(f, 'inventory', return_value=inventory), patch.object(f, 'run', side_effect=['abc', ' M app.swift']), patch.object(f.subprocess, 'Popen'):
            return f.start(self.args())

    def test_test_job_has_no_build_counter_and_accepts_dirty_checkout(self):
        job = self.launch()
        self.assertTrue(job['dirty'])
        self.assertEqual(job['build'], '')
        self.assertEqual(f.query('SELECT * FROM counters'), [])
        with patch.object(f, 'run', return_value='abc'):
            f.worker(job['id'])
        result = f.get_job(job['id'])
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['stage'], 'Tests complete')
        self.assertIn('test-output', Path(result['log']).read_text())
        self.assertEqual(f.query('SELECT * FROM leases'), [])

    def test_test_job_rejects_release_options(self):
        with patch.object(f, 'project', return_value=self.p):
            for args in (self.args(skip_tests=True), self.args(build='1.2.3')):
                with self.assertRaisesRegex(ValueError, 'do not accept'): f.start(args)

    def test_concurrency_requires_distinct_worktrees_and_devices(self):
        self.launch()
        with self.assertRaisesRegex(ValueError, 'conflicting'): self.launch(device='phone2')
        other = dict(self.p, root=str(self.root / 'other'))
        Path(other['root']).mkdir()
        with self.assertRaisesRegex(ValueError, 'already leased'): self.launch(other)
        second = self.launch(other, device='phone2')
        self.assertEqual(len(f.query('SELECT * FROM leases')), 2)
        self.assertEqual(second['status'], 'queued')

    def test_test_job_blocked_by_active_release(self):
        self.job()
        with self.assertRaisesRegex(ValueError, 'conflicting'): self.launch()

    def test_failure_releases_lease_and_preserves_log(self):
        job = self.launch()
        (self.repo / '.flightdeck/test.sh').write_text('echo failing-test\nexit 9\n')
        with patch.object(f, 'run', return_value='abc'): f.worker(job['id'])
        result = f.get_job(job['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['exitCode'], 9)
        self.assertEqual(f.query('SELECT * FROM leases'), [])
        self.assertIn('failing-test', Path(result['log']).read_text())

    def test_cancelled_test_never_runs_and_releases_lease(self):
        job = self.launch()
        (f.STATE / 'runs' / job['id'] / 'cancel').touch()
        f.worker(job['id'])
        self.assertEqual(f.get_job(job['id'])['status'], 'cancelled')
        self.assertEqual(f.query('SELECT * FROM leases'), [])

    def test_running_test_cancellation_stops_processes_before_releasing(self):
        job = self.launch()
        (self.repo / '.flightdeck/test.sh').write_text('echo running-test\nsleep 30\necho should-not-finish\n')
        with patch.object(f, 'run', return_value='abc'), ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(f.worker, job['id'])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                log = Path(job['log'])
                if log.exists() and 'running-test' in log.read_text(): break
                time.sleep(.05)
            self.assertEqual(len(f.query('SELECT * FROM leases')), 1)
            f.cancel(job['id'])
            task.result(timeout=15)
        result = f.get_job(job['id'])
        self.assertEqual(result['status'], 'cancelled', result)
        self.assertNotIn('should-not-finish', Path(result['log']).read_text())
        self.assertEqual(f.query('SELECT * FROM leases'), [])

    def test_test_preflight_does_not_require_clean_release_checkout(self):
        folder = self.repo / '.flightdeck'; folder.mkdir()
        (folder / 'test.sh').write_text('# FLIGHTDECK_TEST_PROTOCOL=1')
        def run(args, *rest):
            if 'status' in args: raise AssertionError('test preflight requested clean status')
            return 'ok'
        with patch.object(f, 'run', side_effect=run), patch.object(f.shutil, 'which', return_value='/tool'):
            self.assertTrue(f.check(self.p, 'test')['ready'])
        (folder / 'test.sh').unlink()
        with patch.object(f, 'run', return_value='ok'), patch.object(f.shutil, 'which', return_value='/tool'):
            self.assertFalse(f.check(self.p, 'test')['ready'])


class AdapterTests(unittest.TestCase):
    def test_all_adapters_run_only_tests_on_explicit_destinations(self):
        for project in ('pitpass', 'patchbridge', 'telemetee', 'cuekeeper', 'forkware'):
            with self.subTest(project=project), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); (root / '.flightdeck').mkdir(); (root / 'ios').mkdir()
                script = root / '.flightdeck/test.sh'; script.write_text(test_adapters.script(project))
                subprocess.run(['/bin/bash', '-n', str(script)], check=True)
                tools = root / 'tools'; tools.mkdir()
                stub = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
with open(os.environ['COMMANDS'], 'a') as f: f.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]])+'\\n')
if Path(sys.argv[0]).name == 'python3' and sys.argv[1] == '-':
    os.execv(os.environ['REAL_PYTHON'], [os.environ['REAL_PYTHON'], *sys.argv[1:]])
if os.environ.get('FAIL_TEST') == '1' and Path(sys.argv[0]).name == 'xcodebuild': sys.exit(7)
'''
                # Fixed interpreter avoids recursively invoking the fake python3 through env.
                stub = stub.replace('#!/usr/bin/env python3', '#!' + sys.executable)
                for name in ('xcodegen', 'swift', 'python3', 'xcodebuild'):
                    tool = tools / name; tool.write_text(stub); tool.chmod(0o755)
                phone = 'AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA'
                ipad = 'BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB'
                env = dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH'], COMMANDS=str(root/'commands'), REAL_PYTHON=sys.executable, FLIGHTDECK_RUN_ID='job', FLIGHTDECK_ARTIFACTS_DIR=str(root/'artifacts'), FLIGHTDECK_EVENTS_FILE=str(root/'events'), TEST_DESTINATION='platform=iOS Simulator,id='+phone, TEST_IPHONE_UDID=phone, TEST_IPAD_UDID=ipad)
                result = subprocess.run(['/bin/bash', str(script)], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                commands = [json.loads(line) for line in (root/'commands').read_text().splitlines()]
                xcode = [cmd for cmd in commands if cmd[0] == 'xcodebuild']
                self.assertTrue(xcode)
                for command in xcode:
                    self.assertIn(command[1], ('test', 'build'))
                    self.assertEqual(command[command.index('-parallel-testing-enabled')+1], 'NO')
                    self.assertTrue(command[command.index('-derivedDataPath')+1].startswith(str(root/'artifacts')))
                    if command[1] == 'test':
                        self.assertIn(command[command.index('-destination')+1], ('platform=iOS Simulator,id='+phone, 'platform=iOS Simulator,id='+ipad))
                self.assertIn('Tests complete', (root/'events').read_text())
                self.assertFalse(any('archive' in cmd or '-exportArchive' in cmd or 'git' == cmd[0] for cmd in commands))
                failed = subprocess.run(['/bin/bash', str(script)], env=dict(env, FAIL_TEST='1'), capture_output=True)
                self.assertEqual(failed.returncode, 7)
                invalid = subprocess.run(['/bin/bash', str(script)], env=dict(env, TEST_DESTINATION='platform=iOS Simulator,name=Phone'), capture_output=True)
                self.assertNotEqual(invalid.returncode, 0)
