#!/bin/bash
# Relocate the repository out of a macOS TCC-protected directory (Blocker 1).
#
# WHY: launchd-spawned processes cannot read ~/Desktop, ~/Documents or ~/Downloads
# without Full Disk Access. Installing the boot-persistence agent from
# ~/Desktop/... yields exit 126 "Operation not permitted" (proven 2026-07-27).
# Relocating is preferred over granting Full Disk Access, which weakens system
# security for every process involved.
#
# SAFE BY DEFAULT: dry-run unless --execute is passed. Copies (never moves) so the
# original stays intact until you delete it yourself.
#
#   bash deploy/migrate_out_of_tcc.sh                 # dry run
#   bash deploy/migrate_out_of_tcc.sh --execute       # perform the copy
#   bash deploy/migrate_out_of_tcc.sh --target ~/cgc  # custom destination
set -uo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_ROOT="$HOME/cgc"
EXECUTE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --execute) EXECUTE=1; shift ;;
    --target)  TARGET_ROOT="$2"; shift 2 ;;
    *) echo "unknown argument: $1"; exit 2 ;;
  esac
done
DEST="$TARGET_ROOT/$(basename "$SRC")"

die() { echo "ABORT: $*" >&2; exit 1; }
say() { echo "  $*"; }

echo "=== TCC MIGRATION $( [ "$EXECUTE" -eq 1 ] && echo '(EXECUTE)' || echo '(DRY RUN)') ==="
say "source     : $SRC"
say "destination: $DEST"

# --- preconditions ---
case "$SRC" in
  *"/Desktop/"*|*"/Documents/"*|*"/Downloads/"*) say "source is TCC-protected: yes (migration warranted)" ;;
  *) die "source is not under a TCC-protected directory; migration is unnecessary" ;;
esac
case "$DEST" in
  *"/Desktop/"*|*"/Documents/"*|*"/Downloads/"*) die "destination is also TCC-protected; pick another target" ;;
esac
[ -z "$(git -C "$SRC" status --porcelain)" ] || die "working tree is not clean; commit or stash first"
[ ! -e "$DEST" ] || die "destination already exists: $DEST"
pgrep -f "[Pp]ython(3)?.*(-m )?app\.main" >/dev/null 2>&1 && die "a bot process is running; stop it before migrating"
pgrep -f forward_paper_keepalive >/dev/null 2>&1 && die "the supervisor is running; stop it before migrating"

NEED_KB=$(du -sk "$SRC" | awk '{print $1}')
AVAIL_KB=$(df -k "$HOME" | tail -1 | awk '{print $4}')
say "payload    : $(( NEED_KB / 1024 )) MiB   free at destination: $(( AVAIL_KB / 1024 )) MiB"
[ "$AVAIL_KB" -gt "$(( NEED_KB + 2097152 ))" ] || die "insufficient free space (need payload + 2 GiB headroom)"

say "commit     : $(git -C "$SRC" rev-parse --short HEAD)  tag: $(git -C "$SRC" describe --tags 2>/dev/null || echo none)"
say "carries    : git history, .env* , state/, data_store/, logs/, reports/, .venv/ excluded (rebuilt)"

if [ "$EXECUTE" -eq 0 ]; then
  echo
  echo "  DRY RUN — nothing was changed. Re-run with --execute to perform the copy."
  echo "  After executing you must, in the NEW location:"
  echo "    1. python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  echo "    2. .venv/bin/python -m pytest tests/ -q            (expect all pass)"
  echo "    3. bash deploy/launchd/install_forward_agent.sh    (expect NO exit 126)"
  echo "    4. reboot and verify the agent starts the engine"
  echo "    5. only then delete the old copy at: $SRC"
  exit 0
fi

# --- execute: copy, never move ---
mkdir -p "$TARGET_ROOT" || die "cannot create $TARGET_ROOT"
say "copying (rsync -a, preserving permissions and timestamps; excluding .venv)..."
rsync -a --exclude '.venv/' "$SRC/" "$DEST/" || die "rsync failed"

# --- post-copy verification ---
echo "=== VERIFICATION ==="
[ -d "$DEST/.git" ] || die "git directory missing at destination"
say "git history: $(git -C "$DEST" rev-list --count HEAD) commits, HEAD=$(git -C "$DEST" rev-parse --short HEAD)"
[ "$(git -C "$DEST" rev-parse HEAD)" = "$(git -C "$SRC" rev-parse HEAD)" ] || die "HEAD mismatch"
say "tree clean : $( [ -z "$(git -C "$DEST" status --porcelain)" ] && echo yes || echo NO)"
for f in .env .env.forward scripts/launch_forward.sh scripts/lib/env_guard.sh deploy/launchd/forward_agent.sh; do
  [ -e "$DEST/$f" ] && say "present    : $f" || say "MISSING    : $f"
done
say "executables: $(find "$DEST/scripts" "$DEST/deploy" -name '*.sh' -perm -u+x 2>/dev/null | wc -l | tr -d ' ') scripts kept +x"
FAILED=0
while IFS= read -r s; do bash -n "$s" 2>/dev/null || { echo "  SYNTAX FAIL: $s"; FAILED=1; }; done < <(find "$DEST/scripts" "$DEST/deploy" -name '*.sh')
[ "$FAILED" -eq 0 ] && say "syntax     : all scripts parse at destination"
say "state/data : state=$(du -sh "$DEST/state" 2>/dev/null | cut -f1) data_store=$(du -sh "$DEST/data_store" 2>/dev/null | cut -f1)"

echo
echo "  COPY COMPLETE. The ORIGINAL IS UNTOUCHED at $SRC"
echo "  Next, in $DEST :"
echo "    1. python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
echo "    2. .venv/bin/python -m pytest tests/ -q"
echo "    3. bash deploy/launchd/install_forward_agent.sh   (expect NO exit 126)"
echo "    4. reboot and verify boot persistence"
echo "    5. only then delete $SRC"
