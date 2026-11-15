#!/bin/sh
# Offline compiler + isolated fake-source tests only. No app/device/simulator launch.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DEV=${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}
if ps -axo comm= | grep -E '(^|/)(swift|swiftc|swift-frontend|xcodebuild|xcbuild)$' >/dev/null; then
    echo 'Another Swift/Xcode build is running; defer this check.' >&2
    exit 75
fi
BUILD=$(mktemp -d "${TMPDIR:-/tmp}/health-background-check.XXXXXX")
trap 'rm -rf "$BUILD"' EXIT HUP INT TERM
SWIFTC="$DEV/Toolchains/XcodeDefault.xctoolchain/usr/bin/swiftc"
export DEVELOPER_DIR="$DEV"
unset SDKROOT
"$SWIFTC" -sdk "$DEV/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk" \
    -target arm64-apple-ios17.0 -swift-version 6 -warnings-as-errors -parse-as-library -typecheck \
    -module-cache-path "$BUILD/ios-modules" "$ROOT/App/HealthBackgroundCoordinator.swift"
"$SWIFTC" -sdk "$DEV/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk" \
    -target "$(uname -m)-apple-macosx14.0" -swift-version 6 -warnings-as-errors -parse-as-library \
    -module-cache-path "$BUILD/mac-modules" "$ROOT/App/HealthBackgroundCoordinator.swift" \
    "$ROOT/scripts/HealthBackgroundTests.swift" -o "$BUILD/HealthBackgroundTests"
"$BUILD/HealthBackgroundTests"
