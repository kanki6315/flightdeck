#!/usr/bin/env python3
"""Install a small, reviewable protocol shim in each repository. Never commits."""
import argparse
import json
from pathlib import Path
import subprocess
import flightdeck as fd

SHIM = '# FlightDeck protocol 1. Sourced after release options have been parsed.\n# Terminal releases do not require the FlightDeck CLI or an event file.\nFLIGHTDECK_PROTOCOL=1\nflightdeck_event() {\n    [ -n "${FLIGHTDECK_EVENTS_FILE:-}" ] || return 0\n    python3 - "$FLIGHTDECK_EVENTS_FILE" "$1" "${build:-}" "${version:-}" "${archive:-}" <<\'FD_EVENT\'\nimport datetime, json, os, sys\npath, stage, build, version, archive = sys.argv[1:]\nwith open(path, \'a\') as stream:\n    stream.write(json.dumps(dict(time=datetime.datetime.now(datetime.timezone.utc).isoformat(), stage=stage, build=build, version=version, archive=os.path.abspath(archive) if archive else \'\')) + \'\\n\')\nFD_EVENT\n}\nflightdeck_require_udid() {\n    if [[ ! "$1" =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]]; then\n        printf \'FlightDeck requires an explicit simulator UUID for %s\\n\' "$2" >&2\n        return 2\n    fi\n}\nif [ -n "${FLIGHTDECK_RUN_ID:-}" ] && [ "${run_tests:-1}" = 1 ]; then\n    case "${TEST_DESTINATION:-}" in\n        \'platform=iOS Simulator,id=\'*) ;;\n        *) echo "FlightDeck requires an explicitly leased simulator destination" >&2; exit 2 ;;\n    esac\n    flightdeck_require_udid "${TEST_DESTINATION#platform=iOS Simulator,id=}" TEST_DESTINATION\n    if [ "${FLIGHTDECK_REQUIRES_IPAD:-0}" = 1 ]; then\n        flightdeck_require_udid "${TEST_IPHONE_UDID:-}" TEST_IPHONE_UDID\n        flightdeck_require_udid "${TEST_IPAD_UDID:-}" TEST_IPAD_UDID\n        if [ "$TEST_IPHONE_UDID" = "$TEST_IPAD_UDID" ] || [ "$TEST_DESTINATION" != "platform=iOS Simulator,id=$TEST_IPHONE_UDID" ]; then\n            echo "FlightDeck requires distinct iPhone/iPad leases and a matching phone destination" >&2\n            exit 2\n        fi\n    fi\n    export TEST_WORKERS=1\n    export TEST_PARALLEL_ENABLED=NO\nfi\n\n# Preserve Xcode\'s existing terminal defaults unless an override is supplied.\nflightdeck_test_args=()\nif [ -n "${TEST_PARALLEL_ENABLED:-}" ]; then\n    flightdeck_test_args=(-parallel-testing-enabled "$TEST_PARALLEL_ENABLED")\nfi\n'


def transformed(p):
    root=Path(p['root']); path=root/p['script']; text=path.read_text()
    if 'FLIGHTDECK_PROTOCOL=1' in text: return {}
    marker='log() { printf'
    pos=text.index(marker)
    relative='../.flightdeck/release.sh' if p['script'].startswith('ios/') else '.flightdeck/release.sh'
    text=text[:pos]+f'# FLIGHTDECK_PROTOCOL=1\nsource "$here/{relative}"\n\n'+text[pos:]
    # Existing log output remains identical, with an additional machine-readable event.
    text=text.replace('log() { printf', 'log() { flightdeck_event "$*"; printf',1)
    if p['id']!='forkware':
        text=text.replace('xcodebuild test \\\n','xcodebuild test \\\n        ${flightdeck_test_args[@]+"${flightdeck_test_args[@]}"} \\\n')
    if p['id']=='forkware':
        text=text.replace('# FLIGHTDECK_PROTOCOL=1', '# FLIGHTDECK_PROTOCOL=1\nFLIGHTDECK_REQUIRES_IPAD=1')
    edits={path:text,root/'.flightdeck/release.sh':SHIM}
    if p['id']=='forkware':
        target=root/'ios/scripts/run_ui_tests.py'; s=target.read_text()
        s=s.replace("parser.add_argument('--iphone', help=", "parser.add_argument('--iphone', default=os.environ.get('TEST_IPHONE_UDID'), help=")
        s=s.replace("parser.add_argument('--ipad', help=", "parser.add_argument('--ipad', default=os.environ.get('TEST_IPAD_UDID'), help=")
        edits[target]=s
    return edits


def main():
    a=argparse.ArgumentParser(description=__doc__); a.add_argument('--apply',action='store_true'); a.add_argument('--project'); args=a.parse_args()
    report=[]
    for p in fd.projects():
        if args.project and p['id']!=args.project: continue
        edits=transformed(p)
        for path,content in edits.items():
            if args.apply:
                path.parent.mkdir(parents=True,exist_ok=True); path.write_text(content)
        report.append(dict(project=p['id'],changed=[str(x) for x in edits],applied=args.apply))
    print(json.dumps(report,indent=2))
if __name__=='__main__': main()
