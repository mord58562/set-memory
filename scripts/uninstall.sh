#!/usr/bin/env bash
# uninstall.sh - Remove Set Memory launchd agent.
#
# What this does:
#   - Unloads and removes the launchd agent plist
#   - Does NOT remove state.db, digest.md, or logs (your data is preserved)
#   - Does NOT uninstall pyrekordbox or sqlcipher (shared deps)
#
# To fully remove all data too:
#   rm -rf ~/Downloads/set-memory/state.db
#   rm -rf ~/Downloads/set-memory/digest.md
#   rm -rf ~/Downloads/set-memory/logs/
#
# Usage:
#   bash scripts/uninstall.sh

set -euo pipefail

PLIST_DST="$HOME/Library/LaunchAgents/com.mord58562.setmemory.plist"

echo "=== Set Memory - Uninstall ==="

if [ -f "$PLIST_DST" ]; then
    echo "Unloading launchd agent..."
    launchctl bootout gui/"$(id -u)" "$PLIST_DST" 2>/dev/null || true
    rm "$PLIST_DST"
    echo "Removed: $PLIST_DST"
else
    echo "Plist not found at $PLIST_DST - nothing to remove."
fi

echo ""
echo "=== Uninstall complete ==="
echo ""
echo "state.db, digest.md, and logs/ have been left intact."
echo "Remove them manually if you want to clean up all data:"
echo "  rm ~/Downloads/set-memory/state.db"
echo "  rm ~/Downloads/set-memory/digest.md"
echo "  rm -rf ~/Downloads/set-memory/logs/"
