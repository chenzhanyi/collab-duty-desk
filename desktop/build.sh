#!/bin/bash
# 一键编译菜单栏应用：swiftc + AppKit，零第三方依赖。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HERE/build/CollabMenuBar.app"
BIN="$APP/Contents/MacOS/CollabMenuBar"
RESOURCES="$APP/Contents/Resources"

mkdir -p "$APP/Contents/MacOS" "$RESOURCES"

echo "[1/3] swiftc 编译 Sources/main.swift"
swiftc \
  -O \
  -swift-version 5 \
  -framework AppKit \
  -framework Foundation \
  "$HERE/Sources/main.swift" \
  -o "$BIN"

echo "[2/3] 写入 Info.plist 与 runner.sh"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>zh_CN</string>
  <key>CFBundleDisplayName</key>
  <string>协作菜单栏</string>
  <key>CFBundleExecutable</key>
  <string>CollabMenuBar</string>
  <key>CFBundleIdentifier</key>
  <string>local.collab.menubar</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>CollabMenuBar</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>CFBundleURLTypes</key>
  <array>
    <dict>
      <key>CFBundleURLName</key>
      <string>local.collab.menubar.control</string>
      <key>CFBundleURLSchemes</key>
      <array>
        <string>collab-menubar</string>
      </array>
    </dict>
  </array>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
  <key>LSUIElement</key>
  <true/>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
PLIST
cp "$HERE/runner.sh" "$RESOURCES/runner.sh"
chmod 755 "$BIN" "$RESOURCES/runner.sh"

if command -v codesign >/dev/null 2>&1; then
  echo "[3/3] ad-hoc codesign"
  codesign --force --sign - "$APP" >/dev/null
else
  echo "[3/3] 未找到 codesign（本地运行仍可用）"
fi

echo
echo "已生成：$APP"
echo "启动：open \"$APP\""
