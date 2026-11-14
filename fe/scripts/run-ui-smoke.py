#!/usr/bin/env python3
"""Run one explicit XCTest UI journey against an empty, isolated synthetic backend.

Never resets the backend or creates/erases a simulator automatically. Use only a
simulator and backend instance explicitly reserved for destructive synthetic tests.
"""
import argparse
import datetime
import json
import os
import pathlib
import platform
import re
import subprocess
import sys
import urllib.request

root = pathlib.Path(__file__).resolve().parents[1]
ui = root / 'evidence/ui'


def developer_directory(environment):
    explicit = environment.get('DEVELOPER_DIR')
    if explicit:
        candidates = [pathlib.Path(explicit)]
    else:
        selected = subprocess.run(['/usr/bin/xcode-select', '-p'], env=environment,
                                  text=True, capture_output=True)
        candidates = ([pathlib.Path(selected.stdout.strip())] if selected.returncode == 0 else [])
        candidates.append(pathlib.Path('/Applications/Xcode.app/Contents/Developer'))
    for candidate in candidates:
        if candidate.suffix == '.app':
            candidate = candidate / 'Contents/Developer'
        if (candidate / 'usr/bin/xcodebuild').is_file() and (candidate / 'Platforms/iPhoneSimulator.platform').is_dir():
            return candidate
    raise SystemExit('No complete Xcode installation found. Set DEVELOPER_DIR to its Contents/Developer directory.')


def pristine_backend():
    with urllib.request.urlopen('http://127.0.0.1:8000/state', timeout=10) as response:
        state = json.load(response)
    if state.get('schema_version') != '1':
        raise SystemExit('Unsupported backend schema; no UI test started.')
    if state.get('observations') != [] or state.get('feedback') != []:
        raise SystemExit('UI tests require an EMPTY isolated synthetic backend. Inspect and reset the authorized test instance first.')
    preferences = state.get('preferences', {})
    sources = state.get('sources', [])
    if (preferences.get('notes') != '' or preferences.get('cloud_consent') is not False
            or preferences.get('enabled_source_ids') != ['demo']
            or len(sources) != 4
            or any(s.get('enabled') or s.get('consent') == 'granted' for s in sources if s.get('id') != 'demo')):
        raise SystemExit('Backend has non-pristine preferences or non-demo source consent; refusing screenshots/UI tests.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('result_name', nargs='?', default='RuntimeSmoke-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    parser.add_argument('test_method', nargs='?', default='testSyntheticUserJourney',
                        choices=['testSyntheticUserJourney', 'testSuppressionDeletionAndPreview'])
    parser.add_argument('--device', help='Explicit dedicated simulator UDID; otherwise DEVICE_ID, then legacy evidence/ui/device-id.txt')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', args.result_name):
        parser.error('result_name must be a simple filename, not a path')
    device = args.device or os.environ.get('DEVICE_ID')
    legacy = ui / 'device-id.txt'
    if not device and legacy.is_file():
        device = legacy.read_text().strip()
    if not device:
        parser.error('Specify --device or DEVICE_ID for a dedicated test simulator. No ignored file is required.')
    if not re.fullmatch(r'[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}', device):
        parser.error('device must be a simulator UDID')
    architecture = platform.machine().lower()
    architecture = {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(architecture, architecture)
    if architecture not in {'arm64', 'x86_64'}:
        parser.error('Unsupported host architecture: ' + architecture)
    env = {key: value for key, value in os.environ.items() if not key.startswith('DYLD_')}
    developer = developer_directory(env)
    for directory in [ui, root/'evidence/modules', root/'evidence/tmp', root/'evidence/UIDerivedData']:
        directory.mkdir(parents=True, exist_ok=True)
    result = ui / (args.result_name + '.xcresult')
    if result.exists():
        parser.error('Result bundle already exists; choose a new name. Prior failures are never overwritten.')
    pristine_backend()
    env['DEVELOPER_DIR'] = str(developer)
    env['PATH'] = str(developer/'usr/bin') + os.pathsep + env.get('PATH', '/usr/bin:/bin')
    env['CLANG_MODULE_CACHE_PATH'] = str(root/'evidence/modules')
    env['TMPDIR'] = str(root/'evidence/tmp')
    # Do not inherit a macOS SDKROOT into an iOS test build.
    env.pop('SDKROOT', None)
    command = [str(developer/'usr/bin/xcodebuild'), '-project', str(root/'LocalCompanion.xcodeproj'),
               '-scheme', 'LocalCompanion', '-destination', 'platform=iOS Simulator,id=' + device,
               '-derivedDataPath', str(root/'evidence/UIDerivedData'), '-resultBundlePath', str(result),
               '-parallel-testing-enabled', 'NO', 'ARCHS=' + architecture, 'ONLY_ACTIVE_ARCH=YES',
               '-only-testing:LocalCompanionUITests/RuntimeSmokeTests/' + args.test_method, 'test']
    with open(ui/(args.result_name + '.txt'), 'w') as output:
        completed = subprocess.run(command, env=env, stdout=output, stderr=subprocess.STDOUT)
    report = {'command': command, 'exit_code': completed.returncode, 'architecture': architecture,
              'privacy_preflight': 'empty backend, no non-demo enabled/consented source, no notes or cloud consent'}
    (ui/(args.result_name + '-command.json')).write_text(json.dumps(report, indent=2) + '\n')
    print('UI xcodebuild exit', completed.returncode)
    return completed.returncode


if __name__ == '__main__':
    sys.exit(main())
