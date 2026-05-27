#!/bin/bash
# Build SetMemory.app from Sources/.
set -euo pipefail

cd "$(dirname "$0")"

APP_NAME="SetMemory"
BUNDLE="$APP_NAME.app"

rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"

cp Info.plist "$BUNDLE/Contents/Info.plist"

# Generate the .icns from the source SVG if Tools/MakeAppIcon.swift exists.
# Falls back silently if not - the build still works, just no icon.
if [ -f "Tools/MakeAppIcon.swift" ]; then
  swift Tools/MakeAppIcon.swift "Resources/AppIcon.icns" 2>/dev/null || true
fi
cp Resources/AppIcon.icns "$BUNDLE/Contents/Resources/AppIcon.icns" 2>/dev/null || true

SOURCES=(
  Sources/Models.swift
  Sources/Theme.swift
  Sources/StateDB.swift
  Sources/AppState.swift
  Sources/MainView.swift
  Sources/Sidebar.swift
  Sources/ContentView.swift
  Sources/SettingsView.swift
  Sources/App.swift
)

swiftc \
  -target arm64-apple-macos13.0 \
  -O \
  -parse-as-library \
  -framework AppKit -framework SwiftUI -framework Combine \
  -lsqlite3 \
  -o "$BUNDLE/Contents/MacOS/$APP_NAME" \
  "${SOURCES[@]}"

# Ad-hoc sign so Gatekeeper accepts it on this machine.
codesign --force --deep --sign - "$BUNDLE"

echo "Built: $(pwd)/$BUNDLE"
