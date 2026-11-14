#!/usr/bin/env python3
"""TEST ONLY: generate isolated simulator project, no packages or product changes.
Run this script then xcodebuild -project Seeder.xcodeproj -target Seeder
-sdk iphonesimulator -configuration Debug -derivedDataPath ... (use scheme
Seeder instead of target when supplying derivedDataPath). Full seed mode requires
an offline-exported recipe in the helper's Documents and --seed-sha256 DIGEST.
Generated project/plists/build output must remain ignored/private.
"""
from pathlib import Path
import hashlib
import plistlib

root = Path(__file__).resolve().parent
objects = []
def uid(name):
    return hashlib.sha256(name.encode()).hexdigest()[:24].upper()
def add(name, body):
    objects.append(f'{uid(name)} = {{ {body} }};')
    return uid(name)
def refs(values):
    return '(' + ','.join(values) + ',)'

source_refs = []
build_refs = []
# Explicit allowlist: no product code or external packages enter this app.
for name in ['Seeder.swift', 'FullSeed.swift']:
    source = add('source-' + name, f'isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {name}; sourceTree = "<group>";')
    source_refs.append(source)
    build_refs.append(add('build-' + name, f'isa = PBXBuildFile; fileRef = {source};'))
product = add('product', 'isa = PBXFileReference; explicitFileType = wrapper.application; path = Seeder.app; sourceTree = BUILT_PRODUCTS_DIR;')
products = add('products', f'isa = PBXGroup; children = {refs([product])}; name = Products; sourceTree = "<group>";')
main = add('main', f'isa = PBXGroup; children = {refs(source_refs + [products])}; sourceTree = "<group>";')
sources = add('sources', f'isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = {refs(build_refs)}; runOnlyForDeploymentPostprocessing = 0;')
frameworks = add('frameworks', 'isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0;')
config = add('config', '''isa = XCBuildConfiguration; name = Debug; buildSettings = {
SDKROOT = iphonesimulator; SUPPORTED_PLATFORMS = iphonesimulator;
IPHONEOS_DEPLOYMENT_TARGET = 17.0; SWIFT_VERSION = 5.0;
PRODUCT_NAME = Seeder; PRODUCT_BUNDLE_IDENTIFIER = org.localcompanion.test.healthseeder;
INFOPLIST_FILE = Info.plist; CODE_SIGN_ENTITLEMENTS = Seeder.entitlements;
CODE_SIGN_STYLE = Automatic; TARGETED_DEVICE_FAMILY = "1,2";
SWIFT_OPTIMIZATION_LEVEL = "-Onone"; CLANG_ENABLE_MODULES = YES;
ONLY_ACTIVE_ARCH = YES; LD_RUNPATH_SEARCH_PATHS = "$(inherited) @executable_path/Frameworks";
};''')
configs = add('configs', f'isa = XCConfigurationList; buildConfigurations = {refs([config])}; defaultConfigurationIsVisible = 0; defaultConfigurationName = Debug;')
target = add('target', f'isa = PBXNativeTarget; buildConfigurationList = {configs}; buildPhases = {refs([sources, frameworks])}; buildRules = (); dependencies = (); name = Seeder; productName = Seeder; productReference = {product}; productType = "com.apple.product-type.application";')
project = add('project', f'isa = PBXProject; attributes = {{ TargetAttributes = {{ {target} = {{ SystemCapabilities = {{ com.apple.HealthKit = {{ enabled = 1; }}; }}; }}; }}; }}; buildConfigurationList = {configs}; compatibilityVersion = "Xcode 14.0"; developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en,Base); mainGroup = {main}; productRefGroup = {products}; projectDirPath = ""; projectRoot = ""; targets = {refs([target])};')
out = root / 'Seeder.xcodeproj'
out.mkdir(exist_ok=True)
(out / 'project.pbxproj').write_text('// !$*UTF8*$!\n{ archiveVersion = 1; classes = {}; objectVersion = 56; objects = {\n' + '\n'.join(objects) + '\n}; rootObject = ' + project + '; }\n')
with (root / 'Info.plist').open('wb') as f:
    plistlib.dump({'CFBundleIdentifier': '$(PRODUCT_BUNDLE_IDENTIFIER)', 'CFBundleExecutable': '$(EXECUTABLE_NAME)', 'CFBundleName': 'Synthetic Health Demo', 'CFBundleDisplayName': 'Synthetic Health Demo', 'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1', 'CFBundleShortVersionString': '1.0', 'UILaunchScreen': {}, 'NSHealthShareUsageDescription': 'Verify only the synthetic samples created by this test helper.', 'NSHealthUpdateUsageDescription': 'Add explicitly approved synthetic test samples. Never modify your real health data.'}, f)
with (root / 'Seeder.entitlements').open('wb') as f:
    plistlib.dump({'com.apple.developer.healthkit': True}, f)
print(out)
