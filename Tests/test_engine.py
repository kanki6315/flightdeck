import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import flightdeck as f
import asc
import integrate

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name).resolve(); self.old=f.STATE; f.STATE=self.root/'state'
        self.repo=self.root/'repo'; self.repo.mkdir()
        self.p=dict(id='test',name='Test',root=str(self.repo),script='release.sh',buildRoot='build',families=['iPhone'])
    def tearDown(self): f.STATE=self.old; self.tmp.cleanup()
    def job(self,key='job',status='running',pid=0,extra=None):
        data=dict(script='release.sh',skipTests=True,devices=[],commit='abc',log=str(f.STATE/'runs'/key/'output.log'),error='',build='',leaseTokens={})
        data.update(extra or {})
        with f.transaction() as c: c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',(key,'test',str(self.repo),'archive',status,'Tests','2020-01-01T00:00:00+00:00',f.now(),pid,json.dumps(data)))
        (f.STATE/'runs'/key).mkdir(parents=True,exist_ok=True)
    def test_concurrent_acquire_has_one_owner(self):
        f.db().close()
        def claim(n):
            try:
                with f.transaction() as c: return f.acquire(c,'device',self.p,str(n))
            except ValueError: return None
        with ThreadPoolExecutor(max_workers=8) as pool: tokens=list(pool.map(claim,range(8)))
        self.assertEqual(sum(t is not None for t in tokens),1)
    def test_wrong_token_and_active_job_cannot_release(self):
        self.job()
        with f.transaction() as c: token=f.acquire(c,'device',self.p,'job')
        args=argparse.Namespace(action='release',device='device',token='wrong')
        with self.assertRaisesRegex(ValueError,'token'): f.lease_action(args)
        args.token=token
        with self.assertRaisesRegex(ValueError,'active'): f.lease_action(args)
        f.update_job('job',status='succeeded'); f.lease_action(args)
        self.assertEqual(f.query('SELECT count(*) FROM leases')[0][0],0)
    def test_recovery_refuses_live_process(self):
        self.job(pid=os.getpid())
        with self.assertRaisesRegex(ValueError,'alive'): f.recover('job')
    def test_recovery_preserves_live_descendant_group(self):
        self.job(extra={'childPID':123})
        with patch.object(os,'killpg',return_value=None):
            with self.assertRaisesRegex(ValueError,'group'): f.recover('job')
    def test_cleanup_only_removes_derived_data(self):
        dd=self.repo/'build/release/1/DerivedData'; dd.mkdir(parents=True); (dd/'cache').write_text('temporary')
        archive=dd.parent/'App.xcarchive'; archive.mkdir(); (archive/'symbols').write_text('retain')
        entry=dict(id='item',eligible=True,path=str(dd))
        with f.transaction() as c: c.execute('INSERT INTO scans VALUES(?,?,?)',('scan',f.now(),json.dumps([entry])))
        with patch.object(f,'projects',return_value=[self.p]),patch.object(f,'run',return_value=''):
            f.cleanup('scan','item')
        self.assertFalse(dd.exists()); self.assertTrue((archive/'symbols').exists())
    def test_cleanup_blocks_symlink_escape_and_active_lease(self):
        outside=self.root/'outside'; outside.mkdir(); (outside/'precious').write_text('keep')
        base=self.repo/'build/release/1'; base.mkdir(parents=True); dd=base/'DerivedData'; dd.symlink_to(outside)
        with f.transaction() as c: c.execute('INSERT INTO scans VALUES(?,?,?)',('scan',f.now(),json.dumps([dict(id='item',eligible=True,path=str(dd))])))
        with patch.object(f,'projects',return_value=[self.p]):
            with self.assertRaisesRegex(ValueError,'Path changed'): f.cleanup('scan','item')
        self.assertTrue((outside/'precious').exists())
        with f.transaction() as c: f.acquire(c,'device',self.p,'agent')
        with self.assertRaisesRegex(ValueError,'active'): f.cleanup('scan','item')
    def test_worker_records_fast_script_events(self):
        script=self.repo/'release.sh'
        script.write_text('''#!/bin/bash
printf '%s\\n' '{"stage":"Archive","archive":"/tmp/example.xcarchive","build":"298.1.1"}' > "$FLIGHTDECK_EVENTS_FILE"
echo complete
''')
        self.job(status='queued')
        with patch.object(f,'run',side_effect=['abc','']): f.worker('job')
        j=f.get_job('job'); self.assertEqual(j['status'],'succeeded'); self.assertEqual(j['archive'],'/tmp/example.xcarchive')
    def test_worker_cancellation_before_start_keeps_script_unrun(self):
        self.job(status='queued'); (f.STATE/'runs/job/cancel').touch()
        f.worker('job'); self.assertEqual(f.get_job('job')['status'],'cancelled')
    def test_worker_rejects_changed_commit(self):
        self.job(status='queued')
        with patch.object(f,'run',return_value='different'): f.worker('job')
        self.assertEqual(f.get_job('job')['status'],'failed')
    def test_asc_matches_marketing_version_and_requires_distribution(self):
        archive=self.repo/'App.xcarchive'; archive.mkdir()
        (archive/'Info.plist').write_bytes(plistlib.dumps({'ApplicationProperties':{'CFBundleIdentifier':'app.test','CFBundleVersion':'1','CFBundleShortVersionString':'2.0'}}))
        payload={'data':[{'id':'build','attributes':{'processingState':'VALID'},'relationships':{'preReleaseVersion':{'data':{'id':'version'}},'buildBetaDetail':{'data':{'id':'beta'}},'betaGroups':{'data':[{'id':'group'}]}}}], 'included':[{'id':'version','attributes':{'version':'2.0'}},{'id':'beta','attributes':{'internalBuildState':'READY_FOR_BETA_TESTING'}},{'id':'group','attributes':{'name':'Internal'}}]}
        job={'archive':str(archive),'root':str(self.repo),'script':'release.sh'}
        with patch.object(asc,'credentials',return_value={}),patch.object(asc,'token',return_value='fake'),patch.object(asc,'fetch',side_effect=[{'data':[{'id':'app'}]},payload]):
            self.assertFalse(asc.status(job)['ready'])
        payload['included'][1]['attributes']['internalBuildState']='IN_BETA_TESTING'
        with patch.object(asc,'credentials',return_value={}),patch.object(asc,'token',return_value='fake'),patch.object(asc,'fetch',side_effect=[{'data':[{'id':'app'}]},payload]): self.assertTrue(asc.status(job)['ready'])
    def test_seed_rejects_active_release_owner(self):
        self.job()
        with f.transaction() as c: token=f.acquire(c,'device',self.p,'job')
        args=argparse.Namespace(project='test',worktree=None,device='device',token=token)
        with patch.object(f,'project',return_value=self.p):
            with self.assertRaisesRegex(ValueError,'release runner'): f.seed(args)
    def test_device_operation_blocks_release_without_blocking_database(self):
        with f.transaction() as c: token=f.acquire(c,'device',self.p,'manual')
        with f.device_lock('device'):
            with f.transaction() as c: c.execute('UPDATE leases SET heartbeat=?',(f.now(),))
            args=argparse.Namespace(action='release',device='device',token=token)
            with self.assertRaisesRegex(ValueError,'operation'): f.lease_action(args)
    def test_terminal_state_waits_for_process_verification(self):
        (self.repo/'release.sh').write_text('#!/bin/bash\necho done\n')
        self.job(status='queued')
        def group_stopped(pid,sig):
            self.assertIn(f.get_job('job')['status'],f.ACTIVE)
            raise ProcessLookupError()
        with patch.object(f,'run',side_effect=['abc','']),patch.object(os,'killpg',side_effect=group_stopped): f.worker('job')
        self.assertEqual(f.get_job('job')['status'],'succeeded')
    def test_integration_keeps_cli_help_and_syntax(self):
        (self.repo/'release.sh').write_text('''#!/bin/bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
run_tests=1
log() { printf '%s\\n' "$*"; }
log test
''')
        for path,content in integrate.transformed(self.p).items(): path.parent.mkdir(parents=True,exist_ok=True); path.write_text(content)
        subprocess.run(['/bin/bash','-n',str(self.repo/'release.sh')],check=True)
        self.assertEqual(subprocess.check_output(['/bin/bash',str(self.repo/'release.sh')],text=True).strip(),'test')
        self.assertEqual(integrate.transformed(self.p),{})

if __name__=='__main__': unittest.main()

class DetachedTests(unittest.TestCase):
    def test_job_survives_launcher_and_can_cancel(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp).resolve(); repo=base/'repo'; repo.mkdir()
            script=repo/'release.sh'
            script.write_text('#!/bin/bash\n# FLIGHTDECK_PROTOCOL=1\necho detached-worker\nsleep 4\necho finished\n')
            subprocess.run(['git','init','-q',str(repo)],check=True)
            subprocess.run(['git','-C',str(repo),'add','.'],check=True)
            subprocess.run(['git','-C',str(repo),'-c','user.name=FlightDeck Test','-c','user.email=test@example.invalid','commit','-qm','fixture'],check=True)
            config=base/'projects.json'; config.write_text(json.dumps([dict(id='forkware',name='Fixture',root=str(repo),script='release.sh',buildRoot='build',families=[])]))
            env=dict(os.environ,FLIGHTDECK_STATE=str(base/'state'),FLIGHTDECK_PROJECTS=str(config))
            def call(*args):
                p=subprocess.run([sys.executable,str(Path(f.__file__)),*args],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                self.assertEqual(p.returncode,0,p.stdout+p.stderr); return json.loads(p.stdout)
            job=call('start','forkware','--skip-tests')
            self.assertEqual(job['mode'],'archive')
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                detail=call('detail',job['id'])
                if detail['status'] not in f.ACTIVE: break
                time.sleep(.1)
            self.assertEqual(detail['status'],'succeeded',detail)
            self.assertIn('finished',detail['output'])
            second=call('start','forkware','--skip-tests')
            self.assertNotEqual(job['build'],second['build'])
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                detail=call('detail',second['id'])
                if detail.get('childPID') and 'detached-worker' in detail.get('output',''): break
                time.sleep(.1)
            call('cancel',second['id'])
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                detail=call('detail',second['id'])
                if detail['status'] not in f.ACTIVE: break
                time.sleep(.1)
            self.assertEqual(detail['status'],'cancelled',detail)


class LauncherTests(unittest.TestCase):
    def test_launcher_resolves_relative_symlink_outside_project(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp).resolve()
            link = folder / 'flightdeck'
            link.symlink_to(os.path.relpath(root / 'flightdeck', folder))
            env = dict(os.environ, FLIGHTDECK_STATE=str(folder / 'state'),
                       FLIGHTDECK_PROJECTS=str(root / 'engine/projects.json'))
            result = subprocess.run([str(link), 'projects'], cwd=folder, env=env,
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn('cuekeeper', [project['id'] for project in json.loads(result.stdout)])
