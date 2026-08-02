#!/bin/bash
# Wrapper used by launchd (or manual runs) to start the trading bot.
# - cd's into the repo so relative paths (.env, logs/, data/) resolve
# - activates ./venv if one exists
# - caffeinate keeps the Mac from idle-sleeping while the bot runs

set -e
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

mkdir -p logs

if command -v caffeinate >/dev/null 2>&1; then
    exec caffeinate -i python3 main.py "$@"
else
    exec python3 main.py "$@"
fi
