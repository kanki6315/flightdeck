#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")" && pwd)"
cache="${FLIGHTDECK_BUILD_DIR:-$root/.build}"
export CLANG_MODULE_CACHE_PATH="$cache/clang-cache"
export SWIFTPM_MODULECACHE_OVERRIDE="$cache/swift-cache"
swift build --disable-sandbox --package-path "$root" --scratch-path "$cache" -c release
app="$root/FlightDeck.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/engine"
cp "$cache/release/FlightDeck" "$app/Contents/MacOS/FlightDeck"
cp "$root/engine/flightdeck.py" "$root/engine/asc.py" "$root/engine/projects.json" "$app/Contents/Resources/engine/"
# Compile the editable Icon Composer document for modern and older macOS.
xcrun actool "$root/Resources/FlightDeck.icon" \
  --compile "$app/Contents/Resources" \
  --output-format human-readable-text --notices --warnings --errors \
  --output-partial-info-plist "$cache/icon-info.plist" \
  --app-icon FlightDeck --include-all-app-icons \
  --enable-on-demand-resources NO --development-region en \
  --target-device mac --minimum-deployment-target 14.0 --platform macosx
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>local.flightdeck.app</string>
<key>CFBundleName</key><string>FlightDeck</string>
<key>CFBundleExecutable</key><string>FlightDeck</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1.0</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
/usr/libexec/PlistBuddy -c "Merge '$cache/icon-info.plist'" "$app/Contents/Info.plist"
xattr -cr "$app"
codesign --force --sign - "$app"
echo "$app"
