"""Parsing for `trade_intents.reason` — and the ban on substring-matching it.

⛔⭐⭐ WHY THIS MODULE EXISTS. Substring matching on reason strings has produced a wrong answer
TWICE IN TWENTY-FOUR HOURS, both times on the same eight characters:

  2026-08-04  `ILIKE '%HARD_STOP%'` swept `HARD_STOP_NATIVE_BACKUP` into a blocked-stop count
              and inflated the headline by 75% (42 episodes reported; 24 was correct).
  2026-08-05  `%hard_stop%` matched `oms_v2_managed_exit:CW_HARD_STOP` while testing whether
              #438's defer queue covered the reverse-reject path. It reported 385/394 "on the
              guard path" when the true answer is 1/394 — and would have routed unbuilt work
              into an attended real-money deploy window.

Three DISTINCT populations share that substring (14-day counts, 2026-08-05):

    oms_v2_managed_exit:CW_HARD_STOP   872   the v2 managed-exit ladder
    HARD_STOP                          542   the OMS software hard stop
    HARD_STOP_NATIVE_BACKUP            471   the native broker-side stop guard

They are different code paths with different failure modes. Pooling them is the
"authoritative for job A is not authoritative for job B" bug in its cheapest form.

⛔ THE RULE: never substring-match a reason. Parse it, then compare the RULE for EQUALITY against
an explicit set. An explicit set is auditable — a reader can see which populations are in scope.
Vigilance already failed twice; this removes the option rather than asking anyone to remember.

⛔ REASON IS NOT UNIFORMLY `emitter:RULE`. Verified against production, three shapes exist:
    "oms_v2_managed_exit:CW_HARD_STOP"        -> ("oms_v2_managed_exit", "CW_HARD_STOP")
    "HARD_STOP_NATIVE_BACKUP"                 -> ("",                    "HARD_STOP_NATIVE_BACKUP")
    "schwab_1m_v2 ATR Flip CW-v2-resting"     -> ("",  "schwab_1m_v2 ATR Flip CW-v2-resting")
A parser that assumes the colon is always present silently mangles the other two.

SQL EQUIVALENT (use this in every ad-hoc query and watch script):

    split_part(reason, ':', 1) AS emitter,
    CASE WHEN reason LIKE '%:%' THEN split_part(reason, ':', 2) ELSE reason END AS rule
    ...
    WHERE rule = ANY(ARRAY['HARD_STOP','HARD_STOP_NATIVE_BACKUP'])   -- explicit, never LIKE

⚠️ This does NOT apply to BROKER reason text (`broker_order_events.payload->>'reason'`). That is
free-form vendor prose ("ORDER_NOT_SUPPORT_REVERSE_OPTION ... (http 417)") with no structure to
parse, so substring matching there is correct — see `_is_reverse_conflict_reject`. Different
field, different contract. Do not "fix" that one to use this.
"""
from __future__ import annotations

from collections.abc import Iterable

__all__ = ["parse_reason", "reason_emitter", "reason_rule", "reason_rule_in"]


def parse_reason(reason: str | None) -> tuple[str, str]:
    """Return `(emitter, rule)`.

    A single leading `emitter:` prefix is split off; anything else is the rule verbatim. Splits on
    the FIRST colon only, so a rule containing a colon survives intact. Never raises.
    """
    text = str(reason or "").strip()
    if not text:
        return "", ""
    emitter, sep, rule = text.partition(":")
    if not sep:
        return "", text
    emitter, rule = emitter.strip(), rule.strip()
    if not emitter or not rule:
        # "  :FOO" / "FOO:" are malformed — treat the whole thing as the rule rather than
        # inventing an empty emitter that would then match an empty-string comparison.
        return "", text
    return emitter, rule


def reason_emitter(reason: str | None) -> str:
    return parse_reason(reason)[0]


def reason_rule(reason: str | None) -> str:
    return parse_reason(reason)[1]


def reason_rule_in(reason: str | None, rules: Iterable[str]) -> bool:
    """EXACT rule membership. The substring-safe replacement for `"HARD_STOP" in reason.upper()`.

    Comparison is case-insensitive on the rule only — reasons are emitted in a consistent case,
    and folding avoids a silent miss if that ever slips.
    """
    rule = reason_rule(reason).upper()
    return bool(rule) and rule in {str(r).strip().upper() for r in rules}
