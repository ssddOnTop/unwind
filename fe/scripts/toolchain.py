#!/usr/bin/env python3
"""Process-local documented Apple paths only. Never changes system setup/licenses."""
import json, os, pathlib, subprocess, sys
root = pathlib.Path(__file__).resolve().parents[1]
evidence = root/'evidence'
dev = pathlib.Path('/Applications/Xcode.app/Contents/Developer')
bin = dev/'Toolchains/XcodeDefault.xctoolchain/usr/bin'
env = {k:v for k,v in os.environ.items() if not k.startswith('DYLD_')}
for key, rel in [('HOME','home'),('CFFIXED_USER_HOME','home'),('TMPDIR','tmp'),('CLANG_MODULE_CACHE_PATH','modules'),('SWIFT_MODULECACHE_PATH','modules'),('XDG_CACHE_HOME','cache')]:
    path=evidence/rel; path.mkdir(parents=True,exist_ok=True); env[key]=str(path)
env['DEVELOPER_DIR']=str(dev)
env['PATH']=str(bin)+':/usr/bin:/bin:/usr/sbin:/sbin'
env['SDKROOT']=str(dev/'Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk')
def run(label, cmd, extra=None):
    e=env.copy(); e.update(extra or {})
    try:
        r=subprocess.run([str(x) for x in cmd],cwd=root,env=e,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    except FileNotFoundError as error:
        r=subprocess.CompletedProcess(cmd,127,'','Toolchain executable unavailable: '+str(error))
    (evidence/(label+'.json')).write_text(json.dumps({'command':[str(x) for x in cmd],'environment':{k:v for k,v in e.items() if k in ['DEVELOPER_DIR','SDKROOT','PATH','HOME','CFFIXED_USER_HOME','TMPDIR','CLANG_MODULE_CACHE_PATH','SWIFT_MODULECACHE_PATH','XDG_CACHE_HOME'] or k.startswith('DYLD_')},'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr},indent=2))
    print(label, 'exit',r.returncode); print(r.stdout); print(r.stderr,file=sys.stderr)
    return r.returncode
mode=sys.argv[1]
if mode in ['test','test-clt']:
    if mode == 'test-clt':
        dev=pathlib.Path('/Library/Developer/CommandLineTools')
        bin=dev/'usr/bin'
        env['DEVELOPER_DIR']=str(dev)
        env['SDKROOT']=str(dev/'SDKs/MacOSX.sdk')
        env['PATH']=str(bin)+':/usr/bin:/bin:/usr/sbin:/sbin'
        frameworks=dev/'Library/Developer/Frameworks'; lib=dev/'usr/lib/swift/macosx'
    else:
        platform=dev/'Platforms/MacOSX.platform/Developer'
        frameworks=platform/'Library/Frameworks'; lib=platform/'usr/lib'
    args=[bin/'swift','test','--package-path',root/'Core','--scratch-path',evidence/('spm-build-clt' if mode == 'test-clt' else 'spm-build'),'--cache-path',evidence/'spm-cache','--config-path',evidence/'spm-config','--security-path',evidence/'spm-security']
    for flag,val in [('-F',frameworks),('-I',lib)]: args+=['-Xswiftc',flag,'-Xswiftc',val]
    for flag,val in [('-F',frameworks),('-L',lib)]: args+=['-Xlinker',flag,'-Xlinker',val]
    sys.exit(run('core-tests-clt' if mode == 'test-clt' else 'core-tests',args,{'DYLD_FRAMEWORK_PATH':str(frameworks),'DYLD_LIBRARY_PATH':str(lib)}))
if mode=='test-standalone':
    import shutil, plistlib
    clt=pathlib.Path('/Library/Developer/CommandLineTools')
    compiler=clt/'usr/bin/swiftc'; frameworks=clt/'Library/Developer/Frameworks'
    sdk=clt/'SDKs/MacOSX.sdk'
    out=evidence/'standalone'; out.mkdir(exist_ok=True)
    bundle=out/'Fixtures.bundle'; bundle.mkdir(exist_ok=True)
    (bundle/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'org.localcompanion.tests','CFBundleName':'Fixtures','CFBundlePackageType':'BNDL'}))
    shutil.copytree(root/'Core/Tests/CompanionCoreTests/Fixtures',bundle/'Fixtures',dirs_exist_ok=True)
    extra={'DEVELOPER_DIR':str(clt),'SDKROOT':str(sdk),'DYLD_FRAMEWORK_PATH':str(frameworks),'DYLD_LIBRARY_PATH':str(out),'COMPANION_TEST_BUNDLE':str(bundle)}
    common=[compiler,'-sdk',sdk,'-target','arm64-apple-macosx14.0','-swift-version','6','-module-cache-path',evidence/'modules-clt','-F',frameworks]
    core=sorted((root/'Core/Sources/CompanionCore').glob('*.swift'))
    code=run('standalone-core-compile',common+['-parse-as-library','-enable-testing','-emit-library','-emit-module','-module-name','CompanionCore','-emit-module-path',out/'CompanionCore.swiftmodule','-o',out/'libCompanionCore.dylib']+core,extra)
    if code: sys.exit(code)
    tests=sorted((root/'Core/Tests/CompanionCoreTests').glob('*.swift'))
    code=run('standalone-tests-compile',common+['-parse-as-library','-I',out,'-L',out,'-lCompanionCore','-Xlinker','-rpath','-Xlinker',out,'-Xlinker','-rpath','-Xlinker',frameworks,'-plugin-path',clt/'usr/lib/swift/host/plugins/testing',root/'scripts/StandaloneTests.swift']+tests+['-o',out/'CoreTests'],extra)
    if code: sys.exit(code)
    sys.exit(run('standalone-tests-run',[out/'CoreTests'],extra))
if mode=='ios':
    results=[]
    for name,platform,target in [('device','iPhoneOS','arm64-apple-ios17.0'),('simulator','iPhoneSimulator','arm64-apple-ios17.0-simulator')]:
        sdk=dev/f'Platforms/{platform}.platform/Developer/SDKs/{platform}.sdk'
        out=evidence/name; out.mkdir(exist_ok=True)
        common=[bin/'swiftc','-sdk',sdk,'-target',target,'-module-cache-path',evidence/'modules','-swift-version','6']
        core=sorted((root/'Core/Sources/CompanionCore').glob('*.swift'))
        app=sorted((root/'App').glob('*.swift'))
        extra={'SDKROOT':str(sdk)}
        result=run(name+'-core',common+['-parse-as-library','-module-name','CompanionCore','-emit-module','-emit-module-path',out/'CompanionCore.swiftmodule','-emit-object','-whole-module-optimization','-o',out/'CompanionCore.o']+core,extra); results.append(result)
        if result: continue
        results.append(run(name+'-typecheck',common+['-I',out,'-typecheck']+app,extra))
        results.append(run(name+'-objects',common+['-I',out,'-emit-object','-whole-module-optimization','-o',out/'CompanionApp.o']+app,extra))
        # Direct Apple compiler/linker path, no enforcement bypass; artifacts remain unsigned.
        linked=run(name+'-link',common+['-I',out,'-o',out/'LocalCompanion',out/'CompanionCore.o']+app,extra)
        results.append(linked)
        if linked == 0:
            import shutil, plistlib
            bundle=out/'LocalCompanion.app'; bundle.mkdir(exist_ok=True)
            shutil.copy2(out/'LocalCompanion',bundle/'LocalCompanion')
            info=plistlib.loads((root/'App/Info.plist').read_bytes())
            info.update({'CFBundleExecutable':'LocalCompanion','CFBundleIdentifier':'org.localcompanion.prototype','CFBundleName':'LocalCompanion','MinimumOSVersion':'17.0','CFBundleSupportedPlatforms':[platform]})
            (bundle/'Info.plist').write_bytes(plistlib.dumps(info))
            shutil.make_archive(str(out/'LocalCompanion-unvalidated'), 'zip', out, 'LocalCompanion.app')
            run(name+'-platform',[bin/'llvm-objdump','--macho','--private-headers',out/'LocalCompanion'],extra)
    sys.exit(1 if any(results) else 0)
if mode=='decode-backend':
    executable=evidence/'decode-backend'
    core=sorted((root/'Core/Sources/CompanionCore').glob('*.swift'))
    code=run('backend-decoder-compile',[bin/'swiftc','-sdk',env['SDKROOT'],'-target','arm64-apple-macosx14.0','-swift-version','6','-module-cache-path',evidence/'modules','-o',executable]+core+[root/'scripts/DecodeBackend.swift'])
    if code: sys.exit(code)
    sys.exit(run('backend-decoder-run',[executable,evidence/'backend-state-responses.json']))
if mode=='xcode':
    sys.exit(run('xcodebuild', [dev/'usr/bin/xcodebuild','-project',root/'LocalCompanion.xcodeproj','-scheme','LocalCompanion','-sdk','iphonesimulator','-configuration','Debug','-derivedDataPath',evidence/'DerivedData','CODE_SIGNING_ALLOWED=NO','build']))
raise SystemExit('unknown mode')
