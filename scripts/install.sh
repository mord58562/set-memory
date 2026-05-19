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
# 4. SQLCipher key (no-op for pyrekordbox >= 0.4)
# ---------------------------------------------------------------------------
echo "[4/8] Checking SQLCipher key access..."
echo "      (pyrekordbox 0.4+ ships the key inside the package; no CDN download needed.)"
"$PYTHON" -c "from pyrekordbox.db6.database import BLOB, deobfuscate; assert deobfuscate(BLOB).startswith('402fd')"
echo "      Key access OK."

# ---------------------------------------------------------------------------
# 5. SQLCipher Python binding (sqlcipher3 ships with pyrekordbox via the
#    sqlcipher3-wheels dependency on macOS arm64; no extra step needed).
# ---------------------------------------------------------------------------
echo "[5/8] Verifying SQLCipher Python binding..."
"$PYTHON" -c "from sqlcipher3 import dbapi2; print('      sqlcipher3 binding OK.')"

# ---------------------------------------------------------------------------
# 6. Verify USB schema (if a rekordbox drive is currently mounted)
# ---------------------------------------------------------------------------
# Same discovery scan the launchd agent uses at runtime. No config to
# consult; whichever drive happens to be plugged in gets checked.
echo "[6/8] Checking rekordbox USB schema..."
PROJECT_ROOT="$PROJECT_ROOT" "$PYTHON" - <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ["PROJECT_ROOT"])
import set_memory, ingest

drives = set_memory.discover_rekordbox_usbs()
if not drives:
    print("      No rekordbox USB mounted - skipping schema check.")
    print("      Plug one in any time to trigger the first sync.")
    sys.exit(0)

usb_master = drives[0]
label = usb_master.parent.parent.parent.name
print(f"      Found {len(drives)} drive(s); checking {label}...")
try:
    key = ingest._get_pyrekordbox_key()
    conn = ingest.connect_master_db(str(usb_master), key=key)
    ingest._require_tables(conn, ['djmdHistory', 'djmdSongHistory', 'djmdContent'])
    count = conn.execute("SELECT COUNT(*) FROM djmdHistory").fetchone()[0]
    print(f"      Schema OK. djmdHistory has {count} row(s).")
    conn.close()
except Exception as e:
    print(f"      Schema check FAILED: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF

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

if [ -s "$PROJECT_ROOT/digest.md" ]; then
    echo "Digest preview:"
    cat "$PROJECT_ROOT/digest.md"
else
    echo "No digest written yet (no rekordbox USB mounted, or no new sessions)."
    echo "Plug in your DJ USB any time to trigger the first sync."
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
