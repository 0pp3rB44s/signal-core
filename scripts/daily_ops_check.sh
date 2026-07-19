#!/bin/bash
# Dagelijkse operationele cyclus in één commando (alleen-lezen).
# Controleert platformgezondheid, datakwaliteit, runtime en tradingstatus.
# Exit 0 = alles PASS; exit 1 = minstens één FAIL (zie regels hieronder).
set -uo pipefail

PROJECT_DIR="${CGC_PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$PROJECT_DIR"
ARCHIVE_DIR="${ARCHIVE_DIR:-$PROJECT_DIR/data/archive}"
FAILURES=0
TODAY="$(date -u +%F)"

note() { printf '%-34s %s\n' "$1" "$2"; }
fail() { printf '%-34s FAIL: %s\n' "$1" "$2"; FAILURES=$((FAILURES + 1)); }

echo "=== DAILY OPS CHECK $(date -u +%FT%TZ) ==="

# 1. Forward-paper-runtime
FP_OUT="$(bash scripts/check_forward_paper.sh 2>&1 || true)"
FP_STATUS="$(printf '%s\n' "$FP_OUT" | awk -F= '$1=="status" {print $2}' | tail -1)"
FP_MODE="$(printf '%s\n' "$FP_OUT" | awk -F= '$1=="mode" {print $2}' | tail -1)"
if [ "$FP_STATUS" = "HEALTHY" ] && [ "$FP_MODE" = "FORWARD_PAPER_ONLY" ]; then
  note "forward_paper" "PASS ($(printf '%s\n' "$FP_OUT" | awk -F= '$1=="scan_cycles_completed" {print $2}' | tail -1) cycli)"
else
  fail "forward_paper" "status=${FP_STATUS:-ONBEKEND} mode=${FP_MODE:-?}"
fi

# 2. Heartbeat-versheid (< 10 min oud)
HB="state/runtime_heartbeat.json"
if [ -f "$HB" ]; then
  AGE=$(( $(date +%s) - $(stat -f %m "$HB") ))
  [ "$AGE" -lt 600 ] && note "bot_heartbeat" "PASS (${AGE}s oud)" || fail "bot_heartbeat" "${AGE}s oud (>600)"
else
  fail "bot_heartbeat" "ontbreekt"
fi

# 3. Archiver-health per bron
if [ -f "$ARCHIVE_DIR/status.json" ]; then
  S_AGE=$(( $(date +%s) - $(stat -f %m "$ARCHIVE_DIR/status.json") ))
  [ "$S_AGE" -lt 120 ] || fail "archiver_heartbeat" "status.json ${S_AGE}s oud (>120)"
  for SRC in orderbook funding liquidations; do
    ST="$(python3 -c "import json;print(json.load(open('$ARCHIVE_DIR/status.json'))['sources'].get('$SRC',{}).get('status','?'))" 2>/dev/null)"
    [ "$ST" = "OK" ] || [ "$ST" = "DISABLED" ] && note "archiver_$SRC" "PASS ($ST)" || fail "archiver_$SRC" "status=$ST"
  done
else
  fail "archiver" "status.json ontbreekt"
fi

# 4. Datakwaliteit vandaag: rijen + duplicaatvrij (dedupe-sleutels uniek)
for SRC in orderbook funding; do
  F="$ARCHIVE_DIR/$SRC/$SRC-$TODAY.jsonl"
  if [ -f "$F" ]; then
    DUP="$(python3 - "$F" <<'EOF'
import json, sys
keys = [json.loads(l).get("_k") for l in open(sys.argv[1])]
print(len(keys) - len(set(keys)))
EOF
)"
    ROWS="$(wc -l < "$F" | tr -d ' ')"
    [ "$DUP" = "0" ] && note "data_$SRC" "PASS ($ROWS rijen, 0 dups)" || fail "data_$SRC" "$DUP duplicaten"
  else
    fail "data_$SRC" "dagbestand ontbreekt"
  fi
done

# 5. Disk
FREE_GB="$(df -g "$ARCHIVE_DIR" | awk 'NR==2 {print $4}')"
[ "${FREE_GB:-0}" -ge 5 ] && note "disk" "PASS (${FREE_GB} GB vrij)" || fail "disk" "slechts ${FREE_GB} GB vrij"

# 6. Tradingstatus: strict mode plaatst geen orders; meld forward-uitkomsten
note "trading" "forward-paper-only (orders onmogelijk); open=$(printf '%s\n' "$FP_OUT" | awk -F= '$1=="open_trades" {print $2}' | tail -1) closed=$(printf '%s\n' "$FP_OUT" | awk -F= '$1=="closed_trades" {print $2}' | tail -1)"

echo "=== RESULTAAT: $([ "$FAILURES" -eq 0 ] && echo ALLES PASS || echo "$FAILURES FAIL(S)") ==="
exit "$([ "$FAILURES" -eq 0 ] && echo 0 || echo 1)"
