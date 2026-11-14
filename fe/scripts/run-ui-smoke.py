#!/usr/bin/env python3
"""Runs only on the explicitly created test simulator and isolated synthetic backend."""
import os, subprocess, pathlib, json, sys
root = pathlib.Path(__file__).resolve().parents[1]
ui = root/'evidence/ui'
env = {k:v for k,v in os.environ.items() if not k.startswith('DYLD_')}
env['DEVELOPER_DIR']='/Applications/Xcode.app/Contents/Developer'
env['CLANG_MODULE_CACHE_PATH']=str(root/'evidence/modules')
env['TMPDIR']=str(root/'evidence/tmp')
name=sys.argv[1] if len(sys.argv)>1 else 'RuntimeSmoke-arm64'
cmd=['/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild','-project',str(root/'LocalCompanion.xcodeproj'),'-scheme','LocalCompanion','-destination','platform=iOS Simulator,id='+ (ui/'device-id.txt').read_text().strip(),'-derivedDataPath',str(root/'evidence/UIDerivedData'),'-resultBundlePath',str(ui/(name+'.xcresult')),'-parallel-testing-enabled','NO','ARCHS=arm64','ONLY_ACTIVE_ARCH=YES','test']
with open(ui/(name+'.txt'),'w') as out:
    p=subprocess.run(cmd,env=env,stdout=out,stderr=subprocess.STDOUT)
(ui/(name+'-command.json')).write_text(json.dumps({'command':cmd,'exit_code':p.returncode},indent=2))
print('UI xcodebuild exit',p.returncode)
sys.exit(p.returncode)
