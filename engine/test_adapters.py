"""Reviewable test-only entry points; deliberately separate from release scripts."""
from integrate import SHIM


def script(project):
    if project not in ('pitpass', 'patchbridge', 'telemetee', 'cuekeeper', 'forkware'):
        raise ValueError('No test adapter for ' + project)
    header = '''#!/bin/bash
set -euo pipefail
# FLIGHTDECK_TEST_PROTOCOL=1
: "${FLIGHTDECK_RUN_ID:?Launch tests through flightdeck start --mode test}"
: "${FLIGHTDECK_ARTIFACTS_DIR:?Missing job artifact directory}"
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
out="$FLIGHTDECK_ARTIFACTS_DIR"
mkdir -p "$out"
'''
    if project == 'forkware': header += 'FLIGHTDECK_REQUIRES_IPAD=1\n'
    header += SHIM + '''
log() { flightdeck_event "$*"; printf '\\n==> %s\\n' "$*"; }
'''
    def xcode(proj, scheme, name, destination='$TEST_DESTINATION', extra='', action='test'):
        return f'''log "{name}"
xcodebuild {action} -project {proj} -scheme '{scheme}' \\
    -destination "{destination}" -derivedDataPath "$out/DerivedData" \\
    -resultBundlePath "$out/{name.replace(' ', '-')}.xcresult" \\
    -parallel-testing-enabled NO -maximum-concurrent-test-simulator-destinations 1 \\
    {extra} CODE_SIGNING_ALLOWED=NO
'''
    body = ''
    if project in ('pitpass', 'forkware'): body += 'cd ios\n'
    if project != 'forkware':
        body += 'log "Generate project"\nxcodegen generate ' + ('--spec ios/project.yml ' if project == 'patchbridge' else '') + '--quiet\n'
    if project == 'pitpass': body += xcode('PitPass.xcodeproj', 'PitPass', 'iPad tests')
    if project == 'patchbridge':
        body += '''log "Swift core tests"
swift test --package-path swift --scratch-path "$out/SwiftBuild"
log "Python bridge tests"
PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest discover -s tests
'''
        body += xcode('ios/Patchbridge.xcodeproj', 'Patchbridge', 'iPhone tests')
    if project == 'telemetee':
        body += '''log "Domain tests"
swift test --package-path Packages/TelemeteeKit --scratch-path "$out/SwiftBuild"
'''
        body += xcode('Telemetee.xcodeproj', 'Telemetee', 'App tests')
        body += '''log "CloudKit tooling tests"
python3 -m unittest discover -s tools/tests
'''
        body += xcode('Telemetee.xcodeproj', 'Telemetee Schema Helper', 'Schema helper build', 'platform=macOS', action='build')
    if project == 'cuekeeper':
        body += '''log "CloudKit tooling and release-gate tests"
python3 -m unittest discover -s scripts/tests
'''
        for target in ('CuekeeperTests', 'CuekeeperUITests'):
            body += xcode('Cuekeeper.xcodeproj', 'Cuekeeper', target, extra=f'-only-testing:{target} -collect-test-diagnostics never')
    if project == 'forkware':
        body += '''log "Swift core tests"
swift test --package-path . --scratch-path "$out/SwiftBuild"
'''
        for family, destination in [('iPhone', '$TEST_IPHONE_UDID'), ('iPad', '$TEST_IPAD_UDID')]:
            body += xcode('Forkware.xcodeproj', 'Forkware', family + ' UI tests', 'platform=iOS Simulator,id=' + destination)
    return header + body + 'log "Tests complete"\n'
