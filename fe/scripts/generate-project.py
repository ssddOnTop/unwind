#!/usr/bin/env python3
"""Dependency-free deterministic Xcode project with explicit Swift sources and local SPM core."""
from pathlib import Path
import hashlib
root=Path(__file__).resolve().parents[1]
project=root/'LocalCompanion.xcodeproj'; project.mkdir(exist_ok=True)
def uid(s): return hashlib.sha256(s.encode()).hexdigest()[:24].upper()
def refs(items): return '('+','.join(items)+',)' if items else '()'
objects=[]
def obj(name,body): objects.append(f'\t\t{uid(name)} = {{ {body} }};'); return uid(name)
files=[]; builds=[]
for source in sorted((root/'App').glob('*.swift')):
    f=obj(source.name,f'isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {source.name}; sourceTree = "<group>";')
    files.append(f); builds.append(obj('build-'+source.name,f'isa = PBXBuildFile; fileRef = {f};'))
for name,kind in [('Info.plist','text.plist.xml'),('LocalCompanion.entitlements','text.plist.entitlements'),('ReportNotifications.entitlements','text.plist.entitlements'),('HealthBackground.entitlements','text.plist.entitlements'),('HealthBackgroundNotifications.entitlements','text.plist.entitlements')]:
    files.append(obj(name,f'isa = PBXFileReference; lastKnownFileType = {kind}; path = {name}; sourceTree = "<group>";'))
app=obj('app-group',f'isa = PBXGroup; children = {refs(files)}; path = App; sourceTree = "<group>";')
product=obj('product','isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = LocalCompanion.app; sourceTree = BUILT_PRODUCTS_DIR;')
products=obj('products',f'isa = PBXGroup; children = {refs([product])}; name = Products; sourceTree = "<group>";')
main=obj('main',f'isa = PBXGroup; children = {refs([app,products])}; sourceTree = "<group>";')
package=obj('package','isa = XCLocalSwiftPackageReference; relativePath = Core;')
core=obj('core-product',f'isa = XCSwiftPackageProductDependency; package = {package}; productName = CompanionCore;')
corebuild=obj('core-build',f'isa = PBXBuildFile; productRef = {core};')
plaidpackage=obj('plaid-package','isa = XCRemoteSwiftPackageReference; repositoryURL = "https://github.com/plaid/plaid-link-ios-spm.git"; requirement = { kind = exactVersion; version = 7.4.0; };')
linkkit=obj('linkkit-product',f'isa = XCSwiftPackageProductDependency; package = {plaidpackage}; productName = LinkKit;')
linkbuild=obj('linkkit-build',f'isa = PBXBuildFile; productRef = {linkkit};')
clerkpackage=obj('clerk-package','isa = XCRemoteSwiftPackageReference; repositoryURL = "https://github.com/clerk/clerk-ios.git"; requirement = { kind = exactVersion; version = 1.7.2; };')
clerkproducts=[]; clerkbuilds=[]
for name in ['ClerkKit','ClerkKitUI']:
    productref=obj(name+'-product',f'isa = XCSwiftPackageProductDependency; package = {clerkpackage}; productName = {name};')
    clerkproducts.append(productref); clerkbuilds.append(obj(name+'-build',f'isa = PBXBuildFile; productRef = {productref};'))
authconfig=obj('auth-config','isa = PBXFileReference; lastKnownFileType = text.xcconfig; path = Config/Defaults.xcconfig; sourceTree = SOURCE_ROOT;')
sources=obj('sources',f'isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = {refs(builds)}; runOnlyForDeploymentPostprocessing = 0;')
frameworks=obj('frameworks',f'isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = {refs([corebuild,linkbuild]+clerkbuilds)}; runOnlyForDeploymentPostprocessing = 0;')
resources=obj('resources','isa = PBXResourcesBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0;')
projectconfigs=[]; targetconfigs=[]
for config in ['Debug','Release']:
    debug=config=='Debug'
    # Match SwiftPM's standard Debug active-destination policy. Omitting this
    # makes the app build both simulator slices while its local package builds one.
    active = 'YES' if debug else 'NO'
    projectconfigs.append(obj('project-'+config,'isa = XCBuildConfiguration; name = '+config+'; buildSettings = { CLANG_ENABLE_MODULES = YES; IPHONEOS_DEPLOYMENT_TARGET = 17.0; SDKROOT = iphoneos; SWIFT_VERSION = 6.0; ONLY_ACTIVE_ARCH = '+active+'; };'))
    settings={'PRODUCT_NAME':'"$(TARGET_NAME)"','PRODUCT_BUNDLE_IDENTIFIER':'org.localcompanion.prototype','INFOPLIST_FILE':'App/Info.plist','GENERATE_INFOPLIST_FILE':'NO','CODE_SIGN_ENTITLEMENTS':'"$(REPORT_NOTIFICATIONS_ENTITLEMENTS)"','CODE_SIGN_STYLE':'Automatic','TARGETED_DEVICE_FAMILY':'"1,2"','SUPPORTED_PLATFORMS':'"iphoneos iphonesimulator"','SUPPORTS_MACCATALYST':'NO','SWIFT_STRICT_CONCURRENCY':'complete','SWIFT_OPTIMIZATION_LEVEL':'"-Onone"' if debug else '"-O"','DEBUG_INFORMATION_FORMAT':'dwarf','LD_RUNPATH_SEARCH_PATHS':'"$(inherited) @executable_path/Frameworks"'}
    if debug: settings['SWIFT_ACTIVE_COMPILATION_CONDITIONS']='DEBUG'
    targetconfigs.append(obj('target-'+config,'isa = XCBuildConfiguration; baseConfigurationReference = '+authconfig+'; name = '+config+'; buildSettings = { '+' '.join(f'{k} = {v};' for k,v in settings.items())+' };'))
pc=obj('project-configs',f'isa = XCConfigurationList; buildConfigurations = {refs(projectconfigs)}; defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;')
tc=obj('target-configs',f'isa = XCConfigurationList; buildConfigurations = {refs(targetconfigs)}; defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;')
target=obj('target',f'isa = PBXNativeTarget; buildConfigurationList = {tc}; buildPhases = {refs([sources,frameworks,resources])}; buildRules = (); dependencies = (); name = LocalCompanion; packageProductDependencies = {refs([core,linkkit]+clerkproducts)}; productName = LocalCompanion; productReference = {product}; productType = "com.apple.product-type.application";')
# Dedicated simulator UI test target; no production launch arguments or test backdoors.
uifiles=[]; uibuilds=[]
for source in sorted((root/'UITests').glob('*.swift')):
    uifile=obj('uitest-'+source.name,f'isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {source.name}; sourceTree = "<group>";')
    uifiles.append(uifile); uibuilds.append(obj('uitest-build-'+source.name,f'isa = PBXBuildFile; fileRef = {uifile};'))
uigroup=obj('uitest-group',f'isa = PBXGroup; children = {refs(uifiles)}; path = UITests; sourceTree = "<group>";')
objects=[entry if not entry.startswith('\t\t'+main+' =') else f'\t\t{main} = {{ isa = PBXGroup; children = {refs([app,uigroup,authconfig,products])}; sourceTree = "<group>"; }};' for entry in objects]
uiproduct=obj('uitest-product','isa = PBXFileReference; explicitFileType = wrapper.cfbundle; path = LocalCompanionUITests.xctest; sourceTree = BUILT_PRODUCTS_DIR;')
uisources=obj('uitest-sources',f'isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = {refs(uibuilds)}; runOnlyForDeploymentPostprocessing = 0;')
uiframeworks=obj('uitest-frameworks','isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0;')
uiconfigs=[]
for config in ['Debug','Release']:
    uiconfigs.append(obj('uitest-'+config,'isa = XCBuildConfiguration; name = '+config+'; buildSettings = { PRODUCT_NAME = "$(TARGET_NAME)"; PRODUCT_BUNDLE_IDENTIFIER = org.localcompanion.prototype.UITests; GENERATE_INFOPLIST_FILE = YES; TEST_TARGET_NAME = LocalCompanion; TARGETED_DEVICE_FAMILY = "1,2"; SWIFT_VERSION = 6.0; SWIFT_OPTIMIZATION_LEVEL = "-Onone"; CODE_SIGN_STYLE = Automatic; };'))
uiconfig=obj('uitest-configs',f'isa = XCConfigurationList; buildConfigurations = {refs(uiconfigs)}; defaultConfigurationIsVisible = 0; defaultConfigurationName = Debug;')
proxy=obj('uitest-proxy',f'isa = PBXContainerItemProxy; containerPortal = {uid("project")}; proxyType = 1; remoteGlobalIDString = {target}; remoteInfo = LocalCompanion;')
dependency=obj('uitest-dependency',f'isa = PBXTargetDependency; target = {target}; targetProxy = {proxy};')
uitarget=obj('uitest-target',f'isa = PBXNativeTarget; buildConfigurationList = {uiconfig}; buildPhases = {refs([uisources,uiframeworks])}; buildRules = (); dependencies = {refs([dependency])}; name = LocalCompanionUITests; productName = LocalCompanionUITests; productReference = {uiproduct}; productType = "com.apple.product-type.bundle.ui-testing";')
rootid=obj('project',f'isa = PBXProject; attributes = {{ LastUpgradeCheck = 2720; TargetAttributes = {{ {target} = {{ CreatedOnToolsVersion = 27.2; SystemCapabilities = {{ com.apple.HealthKit = {{ enabled = 1; }}; }}; }}; }}; }}; buildConfigurationList = {pc}; compatibilityVersion = "Xcode 14.0"; developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en,Base); mainGroup = {main}; packageReferences = {refs([package,plaidpackage,clerkpackage])}; productRefGroup = {products}; projectDirPath = ""; projectRoot = ""; targets = {refs([target,uitarget])};')
(project/'project.pbxproj').write_text('// !$*UTF8*$!\n{\n\tarchiveVersion = 1;\n\tclasses = {};\n\tobjectVersion = 56;\n\tobjects = {\n'+'\n'.join(objects)+'\n\t};\n\trootObject = '+rootid+';\n}\n')
schemes=project/'xcshareddata/xcschemes'; schemes.mkdir(parents=True,exist_ok=True)
ref=f'<BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{target}" BuildableName="LocalCompanion.app" BlueprintName="LocalCompanion" ReferencedContainer="container:LocalCompanion.xcodeproj"/>'
(schemes/'LocalCompanion.xcscheme').write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion="2720" version="1.3">
  <BuildAction parallelizeBuildables="YES" buildImplicitDependencies="YES"><BuildActionEntries><BuildActionEntry buildForTesting="YES" buildForRunning="YES" buildForProfiling="YES" buildForArchiving="YES" buildForAnalyzing="YES">{ref}</BuildActionEntry></BuildActionEntries></BuildAction>
  <TestAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" shouldUseLaunchSchemeArgsEnv="YES"><Testables><TestableReference skipped="NO"><BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{uitarget}" BuildableName="LocalCompanionUITests.xctest" BlueprintName="LocalCompanionUITests" ReferencedContainer="container:LocalCompanion.xcodeproj"/></TestableReference></Testables></TestAction>
  <LaunchAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" launchStyle="0" useCustomWorkingDirectory="NO" ignoresPersistentStateOnLaunch="NO" debugDocumentVersioning="YES" debugServiceExtension="internal" allowLocationSimulation="YES"><BuildableProductRunnable runnableDebuggingMode="0">{ref}</BuildableProductRunnable></LaunchAction>
  <ProfileAction buildConfiguration="Release" shouldUseLaunchSchemeArgsEnv="YES" savedToolIdentifier="" useCustomWorkingDirectory="NO" debugDocumentVersioning="YES"><BuildableProductRunnable runnableDebuggingMode="0">{ref}</BuildableProductRunnable></ProfileAction>
  <AnalyzeAction buildConfiguration="Debug"/>
  <ArchiveAction buildConfiguration="Release" revealArchiveInOrganizer="YES"/>
</Scheme>
''')
