#!/usr/bin/env python3
import datetime, json, re, subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[1]
e=root/'evidence'
def load(name): return json.loads((e/(name+'.json')).read_text())
tests=load('core-tests')
match=re.search(r'Test run with (\d+) tests.*passed',tests['stdout'])
assert tests['exit_code']==0 and match, 'Actual nonzero Swift Testing pass is required'
phases={}
for platform in ['device','simulator']:
    phases[platform]={name:load(platform+'-'+name)['exit_code'] for name in ['core','typecheck','objects','link','platform']}
    assert all(code==0 for code in phases[platform].values())
    assert 'minos 17.0' in load(platform+'-platform')['stdout']
plist_paths=[root/'LocalCompanion.xcodeproj/project.pbxproj',root/'App/Info.plist',root/'App/LocalCompanion.entitlements']
lint=subprocess.run(['/usr/bin/plutil','-lint']+[str(p) for p in plist_paths],text=True,capture_output=True)
assert lint.returncode==0,lint.stderr
(e/'project-lint.txt').write_text(lint.stdout+lint.stderr)
project=plist_paths[0].read_text()
sources=sorted(p.name for p in (root/'App').glob('*.swift'))
assert all('path = '+name+';' in project for name in sources),'Project omitted a Swift source'
report={
    'verified_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'macos_swift_testing':{'passed':int(match.group(1)),'ios_runtime_test':False},
    'swift_encoded_payload_schema_validation':load('contract-validation')['passed'],
    'backend_asgi_interop_checks':load('backend-interop')['passed'],
    'real_backend_snapshots_decoded_by_swift':json.loads(load('backend-decoder-run')['stdout'])['backend_snapshots_decoded'],
    'complete_ios_compilation_exit_codes':phases,
    'explicit_app_sources':sources,
    'project_plist_lint':'passed',
    'artifacts':{'device':'device/LocalCompanion-unvalidated.zip','simulator':'simulator/LocalCompanion-unvalidated.zip'},
    'xcodebuild_exit_code':load('xcodebuild')['exit_code'],
    'launch_blockers':['xcodebuild CoreDevice/Mercury _XPCTypeBool loader failure','Prior simctl license warning; no license acceptance or post-update license verification','No simulator install/launch or UI test performed','Device signing, HealthKit entitlement, actual reads and local-backend connectivity unverified'],
    'limitations':['macOS core tests are not iOS runtime tests','ASGI interop does not exercise iOS URLSession or ATS','Seven completed days only; empty HealthKit results do not establish permission or deletion','Device protection/backup enforcement and notification delivery remain untested'],
}
(e/'verification-summary.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
