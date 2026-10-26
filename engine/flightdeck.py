#!/usr/bin/env python3
"""FlightDeck: durable local iOS jobs. All public commands emit JSON."""
import argparse
import contextlib
import datetime as dt
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get('FLIGHTDECK_STATE', str(Path.home() / 'Library/Application Support/FlightDeck'))).expanduser()
ACTIVE = ('queued', 'running', 'cancelling', 'interrupted')
ENV = dict(os.environ, PATH='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:' + os.environ.get('PATH', ''), PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1')


def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def ident(): return uuid.uuid4().hex

def run(args, cwd=None, timeout=60, pass_fds=()):
    p = subprocess.Popen(args, cwd=cwd, env=ENV, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=pass_fds, start_new_session=True)
    try: output,error=p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try: os.killpg(p.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        p.communicate()
        raise ValueError(f'{args[0]} timed out after {timeout} seconds; its process group was stopped.') from None
    if p.returncode: raise ValueError((error or output).strip()[-3000:] or f'{args[0]} exited {p.returncode}')
    return output.strip()


def db():
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    c = sqlite3.connect(STATE / 'state.sqlite', timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, project TEXT, root TEXT, mode TEXT, status TEXT, stage TEXT, created TEXT, updated TEXT, pid INTEGER, data TEXT);
    CREATE TABLE IF NOT EXISTS leases(device TEXT PRIMARY KEY, token TEXT, project TEXT, worktree TEXT, owner TEXT, created TEXT, heartbeat TEXT, profile TEXT);
    CREATE TABLE IF NOT EXISTS seeds(device TEXT PRIMARY KEY, project TEXT, profile TEXT, revision TEXT, updated TEXT);
    CREATE TABLE IF NOT EXISTS counters(project TEXT PRIMARY KEY, value INTEGER);
    CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY, created TEXT, data TEXT);
    ''')
    return c


def query(sql, params=()):
    c=db()
    try: return c.execute(sql,params).fetchall()
    finally: c.close()


@contextlib.contextmanager
def transaction():
    c = db()
    try:
        c.execute('BEGIN IMMEDIATE')
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally: c.close()


def projects():
    config = Path(os.environ.get('FLIGHTDECK_PROJECTS', str(STATE / 'projects.json')))
    items = json.loads((config if config.exists() else HERE / 'projects.json').read_text())
    for p in items: p['root'] = str(Path(p['root']).expanduser().resolve())
    return items


def project(key, root=None):
    p = next((p.copy() for p in projects() if p['id'] == key), None)
    if not p: raise ValueError('Unknown project: ' + key)
    if root:
        candidate = Path(root).expanduser().resolve()
        # A supplied worktree must belong to this repository, not merely contain a script.
        expected = run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], p['root'])
        actual = run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], candidate)
        if Path(expected).resolve() != Path(actual).resolve(): raise ValueError('Worktree does not belong to the selected project.')
        p['root'] = str(candidate)
    return p


def check(p, mode='archive'):
    checks = []
    def add(name, fn):
        try: checks.append(dict(name=name, ok=True, detail=str(fn())))
        except Exception as e: checks.append(dict(name=name, ok=False, detail=str(e)))
    root = Path(p['root'])
    add('Repository', lambda: run(['git', 'rev-parse', '--show-toplevel'], root))
    add('Commit', lambda: run(['git', 'rev-parse', 'HEAD'], root))
    def clean():
        changes = run(['git', 'status', '--porcelain'], root)
        if changes: raise ValueError('Working tree has changes. Commit them before releasing.\n' + changes[:1500])
        return 'Clean'
    if mode != 'test': add('Working tree', clean)
    def script():
        path = root / ('.flightdeck/test.sh' if mode == 'test' else p['script'])
        if not path.is_file(): raise ValueError('Missing job script: ' + str(path))
        if ('FLIGHTDECK_TEST_PROTOCOL=1' if mode == 'test' else 'FLIGHTDECK_PROTOCOL=1') not in path.read_text(): raise ValueError('FlightDeck integration is not installed.')
        return str(path)
    add('Test script' if mode == 'test' else 'Release script', script)
    add('Xcode', lambda: run(['xcodebuild', '-version']))
    for tool in ['python3', 'xcodegen']:
        def exists(t=tool):
            path = shutil.which(t, path=ENV['PATH'])
            if not path: raise ValueError(f'{t} is not installed or on PATH')
            return path
        # Forkware deliberately does not regenerate its Xcode project.
        if tool != 'xcodegen' or p['id'] != 'forkware': add(tool, exists)
    return dict(project=p, checks=checks, ready=all(x['ok'] for x in checks), note='Signing, provisioning, project tests and CloudKit checks run inside the project release script.')


def inventory():
    result = json.loads(run(['xcrun', 'simctl', 'list', '--json']))
    types = {t['identifier']: t.get('productFamily', t.get('name', '')) for t in result.get('devicetypes', [])}
    leases = {r['device']: dict(r) for r in query('SELECT * FROM leases')}
    seeds = {r['device']: dict(r) for r in query('SELECT * FROM seeds')}
    devices = []
    for runtime, rows in result.get('devices', {}).items():
        for d in rows:
            if d.get('isAvailable'):
                devices.append(dict(id=d['udid'], name=d['name'], state=d['state'], runtime=runtime.split('SimRuntime.')[-1], family=types.get(d.get('deviceTypeIdentifier'), ''), lease=leases.get(d['udid']), seed=seeds.get(d['udid'])))
    return devices


def acquire(c, device, p, owner, profile='retained'):
    token = ident()
    try: c.execute('INSERT INTO leases VALUES(?,?,?,?,?,?,?,?)', (device, token, p['id'], p['root'], owner, now(), now(), profile))
    except sqlite3.IntegrityError: raise ValueError('Device is already leased. Release its existing lease first: ' + device)
    return token


@contextlib.contextmanager
def device_lock(device):
    directory=STATE/'locks'; directory.mkdir(parents=True,exist_ok=True)
    path=directory/(hashlib.sha256(device.encode()).hexdigest()+'.lock')
    with path.open('a') as lock:
        try: fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('A device operation is still running. Wait before changing its lease or state.')
        # Closing this descriptor releases ownership only after inherited child descriptors close.
        yield lock.fileno()


def lease_action(args):
    if args.action == 'list': return [dict(r) for r in query('SELECT * FROM leases')]
    if args.action == 'acquire':
        p = project(args.project, args.worktree)
        device = next((d for d in inventory() if d['id'] == args.device), None)
        if not device: raise ValueError('Device is unavailable.')
        with device_lock(args.device), transaction() as c: token = acquire(c, args.device, p, args.owner)
        return dict(device=args.device, token=token, destination='platform=iOS Simulator,id=' + args.device)
    with device_lock(args.device) if args.action=='release' else contextlib.nullcontext():
        with transaction() as c:
            row = c.execute('SELECT * FROM leases WHERE device=? AND token=?', (args.device, args.token)).fetchone()
            if not row: raise ValueError('Lease token does not match.')
            if args.action == 'release':
                job = c.execute('SELECT status FROM jobs WHERE id=?', (row['owner'],)).fetchone()
                if job and job['status'] in ACTIVE: raise ValueError('The owning job is active or needs recovery. Resolve it before releasing its devices.')
                c.execute('DELETE FROM leases WHERE device=?', (args.device,))
            else: c.execute('UPDATE leases SET heartbeat=? WHERE device=?', (now(), args.device))
    return dict(ok=True)


def job_record(row):
    d = dict(row); d.update(json.loads(d.pop('data'))); return d


def jobs(): return [job_record(r) for r in query('SELECT * FROM jobs ORDER BY created DESC LIMIT 100')]


def get_job(key):
    rows = query('SELECT * FROM jobs WHERE id=?', (key,))
    row = rows[0] if rows else None
    if not row: raise ValueError('Unknown job')
    return job_record(row)


def update_job(key, **fields):
    with transaction() as c:
        row = c.execute('SELECT * FROM jobs WHERE id=?', (key,)).fetchone()
        data = json.loads(row['data'])
        for k, v in fields.items():
            if k in ('status', 'stage', 'pid'): c.execute(f'UPDATE jobs SET {k}=? WHERE id=?', (v, key))
            else: data[k] = v
        c.execute('UPDATE jobs SET data=?,updated=? WHERE id=?', (json.dumps(data), now(), key))


def event(key, kind, **extra):
    path = STATE / 'runs' / key / 'events.jsonl'
    with path.open('a') as f: f.write(json.dumps(dict(time=now(), kind=kind, **extra)) + '\n')


def allocate_build(c, p):
    # A logical minute counter preserves 4/2/2 formatting even for multiple runs per minute.
    epoch=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
    value=int((dt.datetime.now(dt.timezone.utc)-epoch).total_seconds()//60)
    row=c.execute('SELECT value FROM counters WHERE project=?',(p['id'],)).fetchone()
    value=max(value,(row['value']+1) if row else value)
    while True:
        days,minutes=divmod(value,1440); hour,minute=divmod(minutes,60)
        build=f'{days+1}.{hour}.{minute}'
        # Also avoid existing artifacts produced by terminal invocations.
        release=Path(p['root'])/p['buildRoot']/'release'
        if not release.exists() or not any(release.glob('*-'+build)): break
        value+=1
    if days+1>9999: raise ValueError('Build-number epoch exhausted; configure a new scheme.')
    c.execute('INSERT OR REPLACE INTO counters VALUES(?,?)',(p['id'],value))
    return build


def start(args):
    p = project(args.project, args.worktree)
    if args.mode == 'test' and (args.skip_tests or args.build):
        raise ValueError('Test jobs do not accept --skip-tests or --build.')
    preflight = check(p, args.mode)
    if not preflight['ready']: raise ValueError('Preflight failed: ' + '; '.join(x['name'] + ': ' + x['detail'] for x in preflight['checks'] if not x['ok']))
    script_name = '.flightdeck/test.sh' if args.mode == 'test' else p['script']
    script = Path(p['root']) / script_name
    # The managed integration is required so destination selection cannot fall back silently.
    if ('FLIGHTDECK_TEST_PROTOCOL=1' if args.mode == 'test' else 'FLIGHTDECK_PROTOCOL=1') not in script.read_text(): raise ValueError('Install the FlightDeck integration for this project first (see README).')
    devices = inventory() if not args.skip_tests else []
    selected = []
    if not args.skip_tests:
        for family in p['families']:
            choices = [d for d in devices if d['family'] == family and not d['lease'] and d['id'] not in selected]
            if args.device: choices = [d for d in choices if d['id'] in args.device]
            if not choices: raise ValueError('No free ' + family + ' simulator. Release a lease or add a compatible device in Device Hub.')
            selected.append(sorted(choices, key=lambda d: (d['state'] != 'Booted', d['name']))[0]['id'])
    key = ident()
    path = STATE / 'runs' / key
    path.mkdir(parents=True)
    commit = run(['git', 'rev-parse', 'HEAD'], p['root'])
    payload = dict(script=script_name, dirty=bool(run(['git', 'status', '--porcelain'], p['root'])), artifacts=str(path/'artifacts'), skipTests=args.skip_tests, devices=selected, commit=commit, log=str(path/'output.log'), build=args.build or '', error='', bundle=p.get('bundle', ''))
    with transaction() as c:
        placeholders = ','.join('?' * len(ACTIVE))
        active_jobs = c.execute(f'SELECT root,mode FROM jobs WHERE status IN ({placeholders})', ACTIVE).fetchall()
        if any(args.mode != 'test' or j['mode'] != 'test' or Path(j['root']).resolve() == Path(p['root']).resolve() for j in active_jobs):
            raise ValueError('A conflicting job is active or needs recovery. Test jobs require separate worktrees; releases run exclusively.')
        tokens = {d: acquire(c, d, p, key, 'test-run') for d in selected}
        payload['leaseTokens'] = tokens
        # Explicit overrides remain available when migrating an existing version train.
        if args.build and not re.fullmatch(r'[1-9][0-9]{0,3}(\.[0-9]{1,2}){0,2}', args.build): raise ValueError('Build number must use up to 4/2/2 digits, for example 298.12.30.')
        if args.mode != 'test' and not payload['build']: payload['build']=allocate_build(c,p)
        c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)', (key,p['id'],p['root'],args.mode,'queued','Waiting',now(),now(),0,json.dumps(payload)))
    with (path/'worker.log').open('a') as output:
        try:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_worker', key], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True, env=ENV)
        except Exception as e:
            update_job(key,status='failed',error=str(e))
            with transaction() as c: c.execute('DELETE FROM leases WHERE owner=?', (key,))
            raise
    return get_job(key)


def worker(key):
    job = get_job(key)
    path = STATE / 'runs' / key
    update_job(key, pid=os.getpid(), status='running', stage='Preflight')
    event(key,'job.started',commit=job['commit'])
    child = None
    final_status, final_stage, final_error = 'failed', 'Failed', ''
    try:
        if (path/'cancel').exists(): raise InterruptedError('Cancelled before starting')
        if run(['git', 'rev-parse', 'HEAD'], job['root']) != job['commit'] or (job['mode'] != 'test' and run(['git', 'status', '--porcelain'], job['root'])):
            raise ValueError('Checkout changed after preflight. Job was not started.')
        args = ['/bin/bash', str(Path(job['root'])/job['script'])]
        if job['mode'] == 'archive': args += ['--no-upload']
        if job['skipTests']: args += ['--skip-tests']
        if job['build']: args += ['--build', job['build']]
        env = dict(ENV, FLIGHTDECK_RUN_ID=key, FLIGHTDECK_EVENTS_FILE=str(path/'script-events.jsonl'), FLIGHTDECK_ARTIFACTS_DIR=str(path/'artifacts'))
        if job['devices']:
            env['TEST_DESTINATION'] = 'platform=iOS Simulator,id=' + job['devices'][0]
            env['TEST_IPHONE_UDID'] = job['devices'][0]
            if len(job['devices']) > 1: env['TEST_IPAD_UDID'] = job['devices'][1]
        # Process groups allow cancellation of xcodebuild and its script children together.
        with (path/'output.log').open('w') as output:
            child = subprocess.Popen(args,cwd=job['root'],env=env,stdout=output,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True)
            update_job(key, childPID=child.pid)
            offset = 0
            while child.poll() is None:
                if (path/'cancel').exists():
                    update_job(key,status='cancelling',stage='Cancelling')
                    os.killpg(child.pid, signal.SIGTERM)
                    try: child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL); child.wait()
                    raise InterruptedError('Tests cancelled.' if job['mode'] == 'test' else 'Cancelled. An upload already accepted by Apple cannot be undone.')
                events = path/'script-events.jsonl'
                if events.exists():
                    with events.open() as source:
                        source.seek(offset)
                        for line in source:
                            try:
                                data = json.loads(line)
                                update_job(key,stage=data['stage'], **{k:v for k,v in data.items() if k in ('archive','version','build') and v})
                                event(key,'stage',stage=data['stage'])
                            except (ValueError,KeyError): pass
                        offset = source.tell()
                with transaction() as c: c.execute('UPDATE leases SET heartbeat=? WHERE owner=?',(now(),key))
                time.sleep(1)
        events = path/'script-events.jsonl'
        if events.exists():
            for line in events.read_text().splitlines():
                try:
                    data=json.loads(line)
                    update_job(key,**{k:v for k,v in data.items() if k in ('archive','version','build') and v})
                except (ValueError,KeyError): pass
        if child.returncode: raise ValueError(f'Job script exited {child.returncode}. See the run log for the failing check.')
        status = 'uploaded' if job['mode'] == 'testflight' else 'succeeded'
        stage = 'Uploaded · Apple processing not yet verified' if status == 'uploaded' else ('Tests complete' if job['mode'] == 'test' else 'Archive complete')
        final_status, final_stage = status, stage
    except InterruptedError as e:
        final_status, final_stage, final_error = 'cancelled', 'Cancelled', str(e)
    except BaseException as e:
        final_status, final_stage, final_error = 'failed', 'Failed', str(e)
    finally:
        if child and child.poll() is None:
            try: os.killpg(child.pid,signal.SIGTERM); child.wait(timeout=10)
            except Exception: pass
        # If descendants may still be alive, retain leases for explicit recovery.
        alive = False
        if child:
            try: os.killpg(child.pid,0); alive=True
            except ProcessLookupError: pass
        if alive: final_status,final_stage,final_error='interrupted','Needs recovery','A child process group is still running. Leases retained.'
        # Publish terminal state and release leases in one transaction, after process verification.
        with transaction() as c:
            row=c.execute('SELECT data FROM jobs WHERE id=?',(key,)).fetchone()
            data=json.loads(row['data']); data['error']=final_error
            data['exitCode']=child.returncode if child else None
            if not alive: c.execute('DELETE FROM leases WHERE owner=?',(key,))
            c.execute('UPDATE jobs SET status=?,stage=?,updated=?,data=? WHERE id=?',(final_status,final_stage,now(),json.dumps(data),key))
        event(key,'job.finished',status=final_status,error=final_error)

    if get_job(key)['status']=='uploaded':
        with (path/'monitor.log').open('a') as output:
            subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'_monitor',key],stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,start_new_session=True,env=ENV)


def monitor(key):
    import asc
    deadline=time.monotonic()+3600
    while time.monotonic()<deadline:
        job=get_job(key)
        if job['status']!='uploaded': return
        try:
            result=asc.status(job)
            update_job(key,stage=result['stage'],apple=result,monitorError='',status='available' if result['ready'] else 'uploaded')
            if result['ready'] or result['processing'] in ('FAILED','INVALID'): return
        except Exception as e:
            update_job(key,monitorError=str(e))
            # Credentials need human setup; don't retry the same configuration every minute.
            if 'credential' in str(e).lower() or 'private key' in str(e).lower(): return
        time.sleep(45)
    update_job(key,monitorError='Automatic checks paused after one hour. Use Check TestFlight Status to refresh.')


def cancel(key):
    job=get_job(key)
    if job['status'] not in ACTIVE: raise ValueError('Job is no longer active.')
    (STATE/'runs'/key/'cancel').touch()
    return dict(ok=True)


def recover(key):
    job=get_job(key)
    if job['status'] not in ACTIVE: raise ValueError('Only unresolved jobs need recovery.')
    if job['status']=='queued' and time.time()-dt.datetime.fromisoformat(job['created']).timestamp()<30: raise ValueError('Worker is still starting. Wait before attempting recovery.')
    if job.get('childPID'):
        try: os.killpg(job['childPID'],0)
        except ProcessLookupError: pass
        else: raise ValueError('The child process group is still alive; resources remain reserved.')
    for pid in [job.get('pid'), job.get('childPID')]:
        if pid:
            try: os.kill(pid,0)
            except ProcessLookupError: continue
            raise ValueError('A recorded process is still alive. Cancel the job first; do not reclaim its resources.')
    with transaction() as c: c.execute('DELETE FROM leases WHERE owner=?',(key,))
    update_job(key,status='failed',stage='Interrupted',error='Recovered after the worker stopped. Check App Store Connect before retrying an upload.')
    return dict(ok=True)


def size(path):
    try: return int(run(['/usr/bin/du','-sk',str(path)],timeout=180).split()[0])*1024
    except Exception: return None


def scan_storage():
    rows=[]
    for p in projects():
        root=Path(p['root'])/p['buildRoot']
        if root.exists(): rows.append(dict(id=ident(), project=p['name'], kind='Project builds', path=str(root), bytes=size(root), eligible=False, reason='Contains archives and build products; inspect individual intermediates below.'))
        releases=root/'release'
        if releases.exists():
            for release in releases.iterdir():
                dd=release/'DerivedData'
                if dd.is_dir() and not dd.is_symlink(): rows.append(dict(id=ident(),project=p['name'],kind='Release intermediates',path=str(dd),bytes=size(dd),eligible=True,reason='Rebuildable DerivedData only. Archive and dSYMs remain.'))
    for name,path in [('Shared DerivedData',Path.home()/'Library/Developer/Xcode/DerivedData'),('Simulator devices',Path.home()/'Library/Developer/CoreSimulator/Devices')]:
        if path.exists(): rows.append(dict(id=ident(),project='Shared',kind=name,path=str(path),bytes=size(path),eligible=False,reason='Shared or persistent state; inspect in Finder / Device Hub.'))
    key=ident()
    with transaction() as c: c.execute('INSERT INTO scans VALUES(?,?,?)',(key,now(),json.dumps(rows)))
    return dict(id=key,rows=rows,note='Parent totals include their children. Do not sum all rows. Unknown sizes indicate access or scan errors.')


def cleanup(scan_id, item_id):
    with transaction() as c:
        if c.execute('SELECT id FROM jobs WHERE status IN (?,?,?,?)',ACTIVE).fetchone() or c.execute('SELECT device FROM leases').fetchone(): raise ValueError('Cleanup is blocked while any job or device lease is active.')
        row=c.execute('SELECT * FROM scans WHERE id=?',(scan_id,)).fetchone()
        if not row: raise ValueError('Scan expired; scan again.')
        entry=next((x for x in json.loads(row['data']) if x['id']==item_id),None)
        if not entry or not entry['eligible']: raise ValueError('This path is not an eligible cleanup candidate.')
        path=Path(entry['path'])
        allowed=False
        for p in projects():
            base=Path(p['root'])/p['buildRoot']/'release'
            if path.name=='DerivedData' and path.parent.parent==base and path.resolve()==path.absolute() and base.resolve()==base.absolute(): allowed=True
        if not allowed or path.is_symlink(): raise ValueError('Path changed since scan; refusing cleanup.')
        # Refuse if an unmanaged build process is running. This is deliberately conservative.
        processes=run(['/bin/ps','-axo','comm='])
        if any(Path(line.strip()).name in ('xcodebuild','swift-build','swift-test') for line in processes.splitlines()): raise ValueError('An external build is running. Wait until it finishes before cleanup.')
        if path.exists(): shutil.rmtree(path)
        c.execute('DELETE FROM scans WHERE id=?',(scan_id,))
    return dict(ok=True,path=str(path))


def validate_manual_lease(c, device, token, p=None):
    row=c.execute('SELECT * FROM leases WHERE device=? AND token=?',(device,token)).fetchone()
    if not row: raise ValueError('A matching lease token is required.')
    if p and (row['project']!=p['id'] or row['worktree']!=p['root']): raise ValueError('A matching project/worktree lease is required.')
    job=c.execute('SELECT status FROM jobs WHERE id=?',(row['owner'],)).fetchone()
    if job and job['status'] in ACTIVE: raise ValueError('The release runner controls this device until its job finishes.')


def seed(args):
    p=project(args.project,args.worktree)
    with device_lock(args.device) as lock_fd:
        with transaction() as c: validate_manual_lease(c,args.device,args.token,p)
        if p['id']!='telemetee': raise ValueError('This project does not yet have an idempotent seed adapter.')
        script=Path(p['root'])/'tools/seed-simulator-course.sh'
        revision=hashlib.sha256(script.read_bytes()+(Path(p['root'])/'backend/curation/solina.json').read_bytes()+(Path(p['root'])/'Packages/TelemeteeKit/Tests/TelemeteeGeometryTests/Fixtures/osm/solina.json').read_bytes()).hexdigest()[:16]
        # The child inherits the per-device lock; unrelated job heartbeats remain unblocked.
        output=run(['/bin/sh',str(script),args.device,p['bundle']],cwd=p['root'],timeout=600,pass_fds=(lock_fd,))
        with transaction() as c: c.execute('INSERT OR REPLACE INTO seeds VALUES(?,?,?,?,?)',(args.device,p['id'],'Solina',revision,now()))
    return dict(ok=True,revision=revision,output=output)


def device_action(args):
    with device_lock(args.device) as lock_fd:
        with transaction() as c: validate_manual_lease(c,args.device,args.token)
        if args.action=='boot':
            device=next((d for d in inventory() if d['id']==args.device),None)
            if not device: raise ValueError('Simulator is unavailable.')
            if device['state']!='Booted': run(['xcrun','simctl','boot',args.device],pass_fds=(lock_fd,))
            run(['xcrun','simctl','bootstatus',args.device,'-b'],timeout=180,pass_fds=(lock_fd,))
        else: run(['xcrun','simctl','shutdown',args.device],pass_fds=(lock_fd,))
    return dict(ok=True)


def parser():
    a=argparse.ArgumentParser(description=__doc__); sub=a.add_subparsers(dest='command',required=True)
    sub.add_parser('projects'); sub.add_parser('jobs'); sub.add_parser('devices'); sub.add_parser('storage')
    p=sub.add_parser('preflight'); p.add_argument('project'); p.add_argument('--worktree'); p.add_argument('--mode', choices=['test','archive','testflight'], default='archive')
    p=sub.add_parser('start'); p.add_argument('project'); p.add_argument('--worktree'); p.add_argument('--mode',choices=['test','archive','testflight'],default='archive'); p.add_argument('--skip-tests',action='store_true'); p.add_argument('--device',action='append'); p.add_argument('--build')
    for name in ['cancel','recover','_worker','detail','asc-status','_monitor']:
        p=sub.add_parser(name); p.add_argument('id')
    p=sub.add_parser('lease'); p.add_argument('action',choices=['list','acquire','release','heartbeat']); p.add_argument('--device'); p.add_argument('--project'); p.add_argument('--worktree'); p.add_argument('--owner',default='manual'); p.add_argument('--token')
    p=sub.add_parser('cleanup'); p.add_argument('scan'); p.add_argument('item'); p.add_argument('--confirm',action='store_true',required=True)
    p=sub.add_parser('device-action'); p.add_argument('action',choices=['boot','shutdown']); p.add_argument('--device',required=True); p.add_argument('--token',required=True)
    p=sub.add_parser('seed'); p.add_argument('project'); p.add_argument('--worktree'); p.add_argument('--device',required=True); p.add_argument('--token',required=True)
    return a


def main():
    args=parser().parse_args()
    try:
        if args.command=='projects': result=projects()
        elif args.command=='jobs': result=jobs()
        elif args.command=='devices': result=inventory()
        elif args.command=='preflight': result=check(project(args.project,args.worktree), args.mode)
        elif args.command=='start': result=start(args)
        elif args.command=='_worker': worker(args.id); return
        elif args.command=='_monitor': monitor(args.id); return
        elif args.command=='cancel': result=cancel(args.id)
        elif args.command=='recover': result=recover(args.id)
        elif args.command=='lease': result=lease_action(args)
        elif args.command=='storage': result=scan_storage()
        elif args.command=='cleanup': result=cleanup(args.scan,args.item)
        elif args.command=='seed': result=seed(args)
        elif args.command=='device-action': result=device_action(args)
        elif args.command=='asc-status':
            import asc
            job=get_job(args.id)
            if job['status'] not in ('uploaded','available'): raise ValueError('Only uploaded jobs have TestFlight status.')
            result=asc.status(job)
            update_job(args.id,stage=result['stage'],apple=result,status='available' if result['ready'] else 'uploaded')
        elif args.command=='detail':
            result=get_job(args.id)
            log=Path(result['log'])
            if log.exists():
                with log.open('rb') as f: f.seek(max(0,log.stat().st_size-60000)); result['output']=f.read().decode('utf-8',errors='replace')
            else: result['output']='Waiting for output…'
        print(json.dumps(result))
    except Exception as e:
        print(json.dumps(dict(error=str(e)))); sys.exit(1)

if __name__=='__main__': main()
