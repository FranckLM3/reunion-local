#!/usr/bin/env bash
# Construit Réunion.app (barre des menus) et l'installe dans ~/Applications.
set -euo pipefail
ICI="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/Réunion.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
swiftc -O -parse-as-library -o "$APP/Contents/MacOS/Reunion" "$ICI/ReunionApp.swift"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>local.reunion.app</string>
  <key>CFBundleName</key><string>Réunion</string>
  <key>CFBundleExecutable</key><string>Reunion</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>15.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Enregistrer ta voix pendant les réunions, transcrite sur ce Mac.</string>
  <key>ReunionDossier</key><string>$ICI</string>
</dict></plist>
PLIST
codesign --force --sign - "$APP"
echo "Installé : $APP"
