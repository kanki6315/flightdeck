"""Read-only App Store Connect build status. Signing uses the installed OpenSSL."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.parse
import urllib.request


def credentials(root, script):
    # Parse only these literal dotenv values. Never execute a credentials file.
    values = {k: os.environ[k] for k in ('ASC_KEY_ID','ASC_ISSUER_ID','ASC_KEY_PATH') if k in os.environ}
    override=os.environ.get('RELEASE_ENV_FILE') or os.environ.get('MEALOS_RELEASE_ENV')
    path=Path(override).expanduser() if override else Path(root)/Path(script).parent/'.release.env'
    if path.exists():
        for line in path.read_text().splitlines():
            match=re.match(r'^\s*(?:export\s+)?(ASC_KEY_ID|ASC_ISSUER_ID|ASC_KEY_PATH)\s*=\s*(.*?)\s*$',line)
            if match:
                value=match[2].strip().strip('\"\'')
                value=value.replace('${HOME}',str(Path.home())).replace('$HOME',str(Path.home()))
                # Only literal paths/IDs; shell expressions are not interpreted.
                if '$(' in value or '`' in value: continue
                values.setdefault(match[1],value)
    if not values.get('ASC_KEY_ID') or not values.get('ASC_ISSUER_ID'):
        raise ValueError('App Store Connect API credentials are not configured. Uploads can still use Xcode; check processing in App Store Connect or configure ASC_KEY_ID, ASC_ISSUER_ID and ASC_KEY_PATH.')
    values.setdefault('ASC_KEY_PATH',str(Path.home()/'.appstoreconnect/private_keys'/('AuthKey_'+values['ASC_KEY_ID']+'.p8')))
    return values


def b64(value): return base64.urlsafe_b64encode(value).rstrip(b'=').decode()

def token(values):
    head=b64(json.dumps(dict(alg='ES256',kid=values['ASC_KEY_ID'],typ='JWT'),separators=(',',':')).encode())
    timestamp=int(time.time())
    body=b64(json.dumps(dict(iss=values['ASC_ISSUER_ID'],iat=timestamp-10,exp=timestamp+600,aud='appstoreconnect-v1'),separators=(',',':')).encode())
    message=(head+'.'+body).encode()
    key=Path(values['ASC_KEY_PATH']).expanduser()
    if not key.is_file(): raise ValueError('The configured App Store Connect private key file is missing.')
    p=subprocess.run(['/usr/bin/openssl','dgst','-sha256','-sign',str(key)],input=message,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if p.returncode: raise ValueError('Unable to sign the App Store Connect request with the configured private key.')
    # ECDSA DER SEQUENCE(INTEGER r, INTEGER s) -> JOSE fixed-width r || s.
    der=p.stdout
    if len(der)<8 or der[0]!=0x30 or der[2]!=2: raise ValueError('Unexpected ECDSA signature format.')
    n=der[3]; r=der[4:4+n]; pos=4+n
    if der[pos]!=2: raise ValueError('Unexpected ECDSA signature format.')
    s=der[pos+2:pos+2+der[pos+1]]
    raw=int.from_bytes(r,'big').to_bytes(32,'big')+int.from_bytes(s,'big').to_bytes(32,'big')
    return message.decode()+'.'+b64(raw)


def fetch(path, bearer, params=None):
    url='https://api.appstoreconnect.apple.com/v1/'+path
    if params: url+='?'+urllib.parse.urlencode(params)
    request=urllib.request.Request(url,headers={'Authorization':'Bearer '+bearer,'Accept':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=30) as response: return json.load(response)
    except urllib.error.HTTPError as error:
        raise ValueError(f'App Store Connect returned HTTP {error.code}. Check the API key role and app access.') from None


def status(job):
    archive=job.get('archive')
    if not archive: raise ValueError('No archive metadata was recorded for this run.')
    import plistlib
    path=Path(archive)/'Info.plist'
    info=plistlib.loads(path.read_bytes()).get('ApplicationProperties',{})
    bundle=info.get('CFBundleIdentifier'); build=info.get('CFBundleVersion'); version=info.get('CFBundleShortVersionString')
    if not bundle or not build: raise ValueError('The archive is missing its app identifier or build number.')
    bearer=token(credentials(job['root'],job['script']))
    apps=fetch('apps',bearer,{'filter[bundleId]':bundle})['data']
    if not apps: raise ValueError('No matching app is accessible to this API key.')
    result=fetch('builds',bearer,{'filter[app]':apps[0]['id'],'filter[version]':build,'include':'buildBetaDetail,preReleaseVersion,betaGroups','limit':'200'})
    included={v['id']:v for v in result.get('included',[])}
    builds=[]
    for b in result['data']:
        rel=b.get('relationships',{}).get('preReleaseVersion',{}).get('data')
        release=included.get(rel['id'],{}) if rel else {}
        if release.get('attributes',{}).get('version')==version: builds.append(b)
    if not builds: return dict(stage='Waiting for Apple to register the build',processing='NOT_FOUND',ready=False)
    b=builds[0]; processing=b['attributes'].get('processingState','UNKNOWN')
    rel=b.get('relationships',{}).get('buildBetaDetail',{}).get('data')
    detail=included.get(rel['id'],{}).get('attributes',{}) if rel else {}
    internal=detail.get('internalBuildState','UNKNOWN'); external=detail.get('externalBuildState','UNKNOWN')
    groups=b.get('relationships',{}).get('betaGroups',{}).get('data',[])
    ready=processing=='VALID' and (internal=='IN_BETA_TESTING' or external=='IN_BETA_TESTING') and bool(groups)
    stage='Available to testers' if ready else 'Apple: '+processing+' · internal '+internal+' · external '+external
    return dict(stage=stage,processing=processing,internal=internal,external=external,ready=ready,groups=[included.get(g['id'],{}).get('attributes',{}).get('name',g['id']) for g in groups],checkedAt=time.time())
