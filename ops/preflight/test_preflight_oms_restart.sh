#!/bin/bash
# KNOWN-BAD TAPE for preflight_oms_restart.sh. Run on the box. Read-only w.r.t. production.
#
# ⛔⭐⭐ WHY A HARNESS AND NOT A GREEN RUN. The fence printed GO against a flat production account on
# its first execution. That proves NOTHING about the case it exists for: GO is also what a totally
# broken fence prints. A safety assertion is not evidence until a deliberately bad tape turns it
# red — and the paths that matter most (stale, unreadable, override-on-unknown) cannot be produced
# on demand in production without opening a real position.
#
# ⇒ It builds a SCRATCH DATABASE (`fence_tape`) with the same three tables and the same real
#   account NAMES, drives the fence through every branch, and drops the scratch DB at the end.
#   Production is only ever READ, and only by the fence itself.
#
# ⛔ The account names are the REAL ones on purpose. Making the fence's account list overridable so
#   a test could point it elsewhere would put a bypass vector into a safety fence; seeding the tape
#   with the real names exercises the real code path instead.
#
# Usage:  sudo bash ops/preflight/test_preflight_oms_restart.sh [path-to-fence]
set -u

FENCE=${1:-/home/trader/ops_preflight/preflight_oms_restart.sh}
ENV_FILE=${PROD_ENV_FILE:-/etc/project-mai-tai/project-mai-tai.env}
[ -x "$FENCE" ] || { echo "fence not executable at $FENCE"; exit 2; }

URL=$(sudo grep -E '^MAI_TAI_DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)
PGU=$(echo "$URL" | sed -E 's|^[^:]+://([^:]+):.*|\1|')
PGP=$(echo "$URL" | sed -E 's|^[^:]+://[^:]+:([^@]+)@.*|\1|')

cleanup() {
  sudo -u postgres dropdb --if-exists fence_tape >/dev/null 2>&1
  rm -f /tmp/fence_tape.env /tmp/fence_bad.env /tmp/fence_noenv.env
}
trap cleanup EXIT

sudo -u postgres dropdb --if-exists fence_tape >/dev/null 2>&1
sudo -u postgres createdb -O "$PGU" fence_tape || { echo "cannot create scratch DB"; exit 2; }
PGPASSWORD="$PGP" psql -q "dbname=fence_tape user=$PGU host=localhost" >/dev/null <<'SQL'
CREATE TABLE broker_accounts (id serial primary key, name varchar(128), provider varchar(32));
CREATE TABLE account_positions (broker_account_id int, symbol varchar(16), quantity numeric, updated_at timestamptz);
CREATE TABLE oms_managed_positions (broker_account_name varchar(128), symbol varchar(16), current_quantity int, status varchar(16));
INSERT INTO broker_accounts (name,provider) VALUES ('live:schwab_1m_v2','schwab'),('live:orb','webull');
SQL
cat > /tmp/fence_tape.env <<ENVF
MAI_TAI_DATABASE_URL=postgresql://$PGU:$PGP@localhost:5432/fence_tape
MAI_TAI_PROTECTED_SYMBOLS=CYN,TE
ENVF
chmod 600 /tmp/fence_tape.env

Q()     { PGPASSWORD="$PGP" psql -q "dbname=fence_tape user=$PGU host=localhost" -c "$1" >/dev/null 2>&1; }
FRESH() { Q "DELETE FROM account_positions; INSERT INTO account_positions VALUES (1,'AAA',0,now()),(2,'BBB',0,now());"; }
RUN()   { MAI_TAI_ENV_FILE=/tmp/fence_tape.env bash "$FENCE" "$@" 2>&1; echo "EXIT=$?"; }
PASS=0; FAILN=0
chk() {
  got=$(echo "$3" | grep -oE 'EXIT=[0-9]+' | tail -1 | cut -d= -f2)
  if [ "$got" = "$2" ]; then echo "  ✅ PASS  $1  (exit $got)"; PASS=$((PASS+1))
  else echo "  ❌ FAIL  $1  want=$2 got=$got"; echo "$3" | sed 's/^/        /'; FAILN=$((FAILN+1)); fi
}

echo "================ FENCE KNOWN-BAD TAPE ================"
FRESH; Q "DELETE FROM oms_managed_positions;"
chk "T1  flat + fresh -> GO"                    0 "$(RUN)"

Q "INSERT INTO oms_managed_positions VALUES ('live:schwab_1m_v2','XHG',10,'open');"
chk "T2  open managed row -> BLOCK"             1 "$(RUN)"

Q "DELETE FROM oms_managed_positions;"; Q "UPDATE account_positions SET quantity=25 WHERE symbol='AAA';"
chk "T3  broker not flat -> BLOCK"              1 "$(RUN)"
chk "T4  override, set MATCHES -> GO"           0 "$(RUN --operator-override 'AAA' --i-accept-naked-position)"
chk "T5  override, set WRONG -> refused"        1 "$(RUN --operator-override 'ZZZ' --i-accept-naked-position)"
chk "T6  override without token -> refused"     1 "$(RUN --operator-override 'AAA')"

# ⛔ The exclusion must come from the ENV, and a manual holding must not block a restart the OMS
# ladder was never covering anyway.
FRESH; Q "UPDATE account_positions SET symbol='CYN', quantity=99 WHERE symbol='AAA';"
chk "T7  operator-manual CYN excluded -> GO"    0 "$(RUN)"

# ⛔⭐ THE LOAD-BEARING PAIR. A stalled sync shows an OLD FLAT BOARD; that must never read as GO,
# and the operator must not be able to wave it through, because the cost is the unknown itself.
FRESH; Q "UPDATE account_positions SET updated_at = now() - interval '2 hours';"
chk "T8  STALE broker positions -> CANNOT SEE"  2 "$(RUN)"
chk "T9  override CANNOT beat blind"            2 "$(RUN --operator-override 'AAA' --i-accept-naked-position)"

FRESH; Q "DELETE FROM account_positions WHERE broker_account_id=2;"
chk "T10 a real account with NO rows -> CANNOT SEE" 2 "$(RUN)"

echo 'MAI_TAI_DATABASE_URL=postgresql://x:y@localhost:5432/nonexistent_db' > /tmp/fence_bad.env
chk "T11 DB unreachable -> CANNOT SEE" 2 "$(MAI_TAI_ENV_FILE=/tmp/fence_bad.env bash "$FENCE" 2>&1; echo "EXIT=$?")"

echo 'NOTHING=1' > /tmp/fence_noenv.env
chk "T12 no DSN in env -> CANNOT SEE"  2 "$(MAI_TAI_ENV_FILE=/tmp/fence_noenv.env bash "$FENCE" 2>&1; echo "EXIT=$?")"

echo "======================================================"
echo "  $PASS passed, $FAILN failed"
[ "$FAILN" -eq 0 ] || exit 1
