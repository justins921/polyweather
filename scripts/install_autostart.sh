#!/bin/bash
# Install the bot as a macOS LaunchAgent so it:
#   - starts automatically when you log in
#   - restarts automatically if it crashes
#
# Usage:   bash scripts/install_autostart.sh
# Stop:    bash scripts/uninstall_autostart.sh
# Status:  launchctl list | grep polyweather
# Logs:    tail -f logs/runner.out

set -e
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.polyweather.bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$REPO_DIR/logs"
chmod +x "$REPO_DIR/scripts/run_bot.sh"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$REPO_DIR/scripts/run_bot.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$REPO_DIR/logs/runner.out</string>
    <key>StandardErrorPath</key>
    <string>$REPO_DIR/logs/runner.err</string>
</dict>
</plist>
EOF

# Reload if already installed, then start
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Installed and started: $LABEL"
echo "  Watch logs:   tail -f $REPO_DIR/logs/runner.out"
echo "  Check status: launchctl list | grep polyweather"
echo "  Stop + remove: bash $REPO_DIR/scripts/uninstall_autostart.sh"
