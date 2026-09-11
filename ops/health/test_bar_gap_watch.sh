#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# TEST the I2/I3 predicates in bar_gap_watch_cron.sh.  Run: bash ops/health/test_bar_gap_watch.sh
#
# ⛔⭐ THE CONTROL THAT MATTERS IS "repair 31 min ago -> GREEN RELEASED". Gating the all-clear is
# easy to over-tighten into never releasing it, which would be a worse failure than the tautological
# green it replaces: the operator would stop getting all-clears and would not know why.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${HERE}/bar_gap_watch_cron.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Lift the predicates out by name — a line range would break on any reorder, silently and vacuously.
extract_fn() {
  awk -v n="$1" '
    !inf && index($0, n"() {") == 1 { print; if ($0 ~ /\}[[:space:]]*$/) next; inf=1; next }
    inf { print; if ($0 ~ /^\}$/) inf=0 }' "$SRC"
}
: > "${TMP}/lib.sh"
for fn in green_held parse_gapped gap_page_due; do
  extract_fn "$fn" >> "${TMP}/lib.sh"
  grep -q "^${fn}() {" "${TMP}/lib.sh" || { echo "EXTRACTION FAILED — ${fn}() not in $SRC"; exit 1; }
done
# shellcheck disable=SC1090
. "${TMP}/lib.sh"

PASS=0; FAIL=0
ok()  { echo "  ✓ $1"; PASS=$((PASS+1)); }
bad() { echo "  ✗ $1"; FAIL=$((FAIL+1)); }
W=1800; NOW=1000000
PAGE_AFTER_SECS=300
[ "$(grep -E '^PAGE_AFTER_SECS=' "$SRC")" = "PAGE_AFTER_SECS=300" ] \
  && ok "shipped unresolved-gap delay is exactly five minutes" \
  || bad "shipped PAGE_AFTER_SECS must remain 300"

echo "TESTING green_held (I2 — a verification must not be satisfiable by our own action)"
# THE INCIDENT: repair at 07:40, green attempted 07:45 -> 5 min later, window still overlaps.
green_held "$NOW" $((NOW-300))  "$W" && ok "repair 5 min ago  -> HELD (the 08-14 tautological green)" \
                                     || bad "repair 5 min ago should be HELD"
green_held "$NOW" $((NOW-1799)) "$W" && ok "repair 29m59s ago -> HELD (still one second inside)" \
                                     || bad "repair 29m59s ago should be HELD"
# ⭐ CONTROL — the gate must RELEASE. An all-clear that never comes is worse than a premature one.
green_held "$NOW" $((NOW-1801)) "$W" && bad "repair 30m01s ago should be RELEASED" \
                                     || ok "repair 30m01s ago -> RELEASED (window has cleared)"
green_held "$NOW" $((NOW-7200)) "$W" && bad "repair 2h ago should be RELEASED" \
                                     || ok "repair 2h ago     -> RELEASED"
# ⛔ THE EXACT BOUNDARY. The repaired bars all predate REPAIR_AT, so a window whose left edge lands
# exactly on REPAIR_AT contains none of them and must RELEASE. Pins -lt against -le.
green_held "$NOW" $((NOW-W))    "$W" && bad "repair exactly ${W}s ago should be RELEASED (-lt, not -le)" \
                                     || ok "repair exactly ${W}s ago -> RELEASED (boundary pinned)"
# never repaired => nothing to hold for; must not block the ordinary all-clear
green_held "$NOW" 0 "$W"             && bad "no repair should NOT hold the green" \
                                     || ok "no repair ever    -> RELEASED (gate inert)"
green_held "$NOW" "" "$W"            && bad "empty state field should NOT hold the green" \
                                     || ok "empty state field -> RELEASED (old 2-field state file)"
# ⛔ The "never repaired" guard is a PRECONDITION, not arithmetic luck. At production epochs
# (now >> window) the subtraction happens to give the right answer without it, so only a small
# clock exposes whether the guard is really there. Pins it as intent rather than coincidence.
green_held 100 0 "$W"                && bad "no-repair guard must not depend on now >> window" \
                                     || ok "no repair, tiny clock -> RELEASED (guard is real, not luck)"

echo "TESTING parse_gapped (I3 — which symbols were holed)"
got=$(printf '%s\n' \
  "[backfill] LBGJ: filled 21/21 missing bar(s) from REST" \
  "[backfill] 2026-08-14: INSERTED 21 bar(s) stamped source='rest'" | parse_gapped)
[ "$got" = "LBGJ " ] && ok "single symbol parsed, date line ignored" || bad "got '$got', want 'LBGJ '"
got=$(printf '%s\n' \
  "[backfill] LBGJ: filled 21/21 missing bar(s) from REST" \
  "[backfill] BOXL: REST fetch FAILED (timeout)" \
  "[backfill] LBGJ: filled 3/3 missing bar(s) from REST" | parse_gapped)
[ "$got" = "BOXL LBGJ " ] && ok "multi-symbol, deduped, includes the FAILED one" || bad "got '$got'"
got=$(printf '%s\n' "nothing to repair" | parse_gapped)
[ -z "$got" ] && ok "no backfill lines -> empty (caller prints UNKNOWN, not a clean zero)" \
              || bad "expected empty, got '$got'"

echo "TESTING gap_page_due (persistent transitions only)"
gap_page_due RED "$NOW" $((NOW-299)) 0 0 0 && bad "299s must not page" \
                                                || ok "299s unresolved -> log only"
gap_page_due RED "$NOW" $((NOW-300)) 0 0 0 && ok "300s unresolved -> page" \
                                                || bad "300s must page"
gap_page_due AMBER "$NOW" $((NOW-600)) 0 0 0 && ok "persistent AMBER -> page" \
                                                   || bad "persistent AMBER must page"
gap_page_due RED "$NOW" $((NOW-600)) 1 0 0 && bad "active incident must not repeat" \
                                                || ok "already paged -> silent"
gap_page_due RED "$NOW" $((NOW-600)) 0 1 0 && bad "halt must not page" \
                                                || ok "market halt -> log only"
gap_page_due GREEN "$NOW" $((NOW-600)) 0 0 0 && bad "recovery must not page" \
                                                  || ok "recovery -> log only"
gap_page_due RED "$NOW" "$NOW" 0 0 1 && ok "selftest bypasses the delay" \
                                             || bad "selftest must exercise delivery"

calls=$(grep -cE '^[[:space:]]*if send_ntfy ' "$SRC" || true)
[ "$calls" -eq 1 ] && ok "wrapper has one delivery call, behind gap_page_due" \
                    || bad "expected one guarded delivery call, found $calls"
grep -q 'OK v2 bar series contiguous again' "$SRC" \
  && bad "recovery title must not exist" || ok "no GREEN recovery delivery remains"
grep -q 'INFO v2 bar gap - market halt' "$SRC" \
  && bad "halt title must not exist" || ok "no market-halt delivery remains"
grep -q -- '--fail-with-body --connect-timeout 10 --max-time 30' "$SRC" \
  && ok "delivery failure and timeout semantics are pinned" \
  || bad "curl must fail on HTTP errors and remain time-bounded"

echo
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
