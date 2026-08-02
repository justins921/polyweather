#!/bin/bash
# Stop the bot and remove the LaunchAgent (the opposite of install_autostart.sh).
# The bot cancels its open orders on shutdown, so this is safe to run any time.

LABEL="com.polyweather.bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ -f "$PLIST" ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm "$PLIST"
    echo "Stopped and removed: $LABEL"
else
    echo "Not installed (no $PLIST found)"
fi
