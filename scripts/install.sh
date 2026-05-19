#!/usr/bin/env bash
# install.sh - Set Memory installation script
#
# What this does:
#   1. Installs system dependencies (sqlcipher via brew)
#   2. Installs pyrekordbox into miniconda3
#   3. Downloads the rekordbox SQLCipher key from Pioneer's CDN (one-time)
#   4. Installs the SQLCipher adapter for pyrekordbox
#   5. Verifies the djmd* table schema on the USB master.db (if mounted)
#   6. Creates the logs/ directory
#   7. Installs the launchd agent plist
#   8. Runs a smoke test
#
# DO NOT run this script with sudo. It installs into the user's miniconda3
# and ~/Library/LaunchAgents, both of which are user-owned.
#
# Prerequisites: Homebrew, miniconda3 at ~/miniconda3/
#
# Usage:
#   bash scripts/install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="$HOME/miniconda3/bin/python3"
PIP="$HOME/miniconda3/bin/pip"
PLIST_SRC="$PROJECT_ROOT/launchd/com.mord58562.setmemory.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.mord58562.setmemory.plist"
LOGS_DIR="$PROJECT_ROOT/logs"

echo "=== Set Memory - Install ==="
echo "Project root: $PROJECT_ROOT"
echo "Python: $PYTHON"
echo ""

# ---------------------------------------------------------------------------
# 1. Verify Python exists
# ---------------------------------------------------------------------------
if [ ! -x "$PYTHON" ]; then
    echo "ERROR: Python not found at $PYTHON"
    echo "Install miniconda3 first: https://docs.conda.io/en/latest/miniconda.html"
    exit 1
fi
echo "[1/8] Python OK: $("$PYTHON" --version)"

# ---------------------------------------------------------------------------
# 2. Install sqlcipher via Homebrew
# ---------------------------------------------------------------------------
echo "[2/8] Installing sqlcipher via Homebrew..."
brew install sqlcipher

# ---------------------------------------------------------------------------
# 3. Install pyrekordbox
# ---------------------------------------------------------------------------
echo "[3/8] Installing pyrekordbox into miniconda3..."
"$PIP" install pyrekordbox==0.4.4

# ---------------------------------------------------------------------------
# 4. Download the rekordbox SQLCipher key from Pioneer's CDN
# ---------------------------------------------------------------------------
echo "[4/8] Downloading rekordbox SQLCipher key from Pioneer CDN..."
echo "      (This is a one-time network call; the key is cached at ~/.pyrekordbox/key)"
"$PYTHON" -m pyrekordbox download-key

# ---------------------------------------------------------------------------
# 5. Install the SQLCipher adapter
# ---------------------------------------------------------------------------
echo "[5/8] Installing SQLCipher adapter for pyrekordbox..."
"$PYTHON" -m pyrekordbox install-sqlcipher

# ---------------------------------------------------------------------------
# 6. Verify USB schema (if USB is mounted and config has a real path)
# ---------------------------------------------------------------------------
# Resolve usb_pioneer_path from config.json so the check follows whatever
# the user has set, rather than a placeholder.
USB_PIONEER_PATH="$(PROJECT_ROOT="$PROJECT_ROOT" "$PYTHON" -c "
import os, sys
sys.path.insert(0, os.environ['PROJECT_ROOT'])
try:
    import config
    print(config.load().usb_pioneer_path)
except Exception:
    print('')
" 2>/dev/null)"
USB_MASTER="${USB_PIONEER_PATH}/Master/master.db"
echo "[6/8] Checking USB master.db schema..."
if [ -n "$USB_PIONEER_PATH" ] && [ -f "$USB_MASTER" ]; then
    echo "      USB master.db found. Verifying djmd* tables..."
    PROJECT_ROOT="$PROJECT_ROOT" USB_MASTER="$USB_MASTER" "$PYTHON" - <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ["PROJECT_ROOT"])
import ingest
import pyrekordbox

try:
    key = ingest._get_pyrekordbox_key()
    conn = ingest.connect_master_db(os.environ["USB_MASTER"], key=key)
    ingest._require_tables(conn, ['djmdHistory', 'djmdSongHistory', 'djmdContent'])
    count = conn.execute("SELECT COUNT(*) FROM djmdHistory").fetchone()[0]
    print(f"      Schema OK. djmdHistory has {count} row(s).")
    conn.close()
except Exception as e:
    print(f"      Schema check FAILED: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
else
    echo "      USB not mounted - skipping schema check."
    echo "      Mount USB and re-run to verify schema before first live sync."
fi

# ---------------------------------------------------------------------------
# 7. Create logs directory
# ---------------------------------------------------------------------------
echo "[7/8] Creating logs directory..."
mkdir -p "$LOGS_DIR"

# ---------------------------------------------------------------------------
# 8. Install launchd agent
# ---------------------------------------------------------------------------
echo "[8/8] Installing launchd agent..."
mkdir -p "$HOME/Library/LaunchAgents"

# Unload existing agent if present (suppress errors if not loaded)
launchctl bootout gui/"$(id -u)" "$PLIST_DST" 2>/dev/null || true

# Render plist with this machine's absolute paths. launchd doesn't expand
# $HOME, so the rendered copy in ~/Library/LaunchAgents holds the only
# concrete user paths; the source template stays portable.
sed -e "s|__PYTHON__|${PYTHON}|g" \
    -e "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g" \
    "$PLIST_SRC" > "$PLIST_DST"
echo "      Plist installed at: $PLIST_DST"

# Load the agent
launchctl bootstrap gui/"$(id -u)" "$PLIST_DST"
echo "      launchd agent loaded."

# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
echo ""
echo "=== Smoke Test ==="
cd "$PROJECT_ROOT"
"$PYTHON" set_memory.py --on-mount
SMOKE_EXIT=$?
echo "Exit code: $SMOKE_EXIT"

if [ -n "$USB_PIONEER_PATH" ] && [ -d "$USB_PIONEER_PATH" ]; then
    echo "Digest preview:"
    cat "$PROJECT_ROOT/digest.md" 2>/dev/null || echo "(no digest.md yet)"
else
    echo "USB not mounted - smoke test exited cleanly (expected)."
    echo "Mount USB to trigger first real sync."
fi

echo ""
echo "=== Install complete ==="
echo ""
echo "Next steps:"
echo "  1. Mount your DJ USB drive."
echo "  2. Set Memory will run automatically and write ~/Downloads/set-memory/digest.md"
echo "  3. Check logs at ~/Downloads/set-memory/logs/ if anything goes wrong."
echo ""
echo "To run the unit test suite:"
echo "  ~/miniconda3/bin/pytest ~/Downloads/set-memory/tests/ -v"
echo ""
echo "To run the smoke test with USB mounted:"
echo "  RUN_SMOKE=1 ~/miniconda3/bin/pytest ~/Downloads/set-memory/tests/test_smoke.py -v"
