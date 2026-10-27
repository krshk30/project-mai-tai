"""New controlled byte-to-episode controls, not historical/native receipts."""

from dataclasses import replace
from decimal import Decimal
import hashlib

from project_mai_tai.atr_simulation.firstbar_reference import (
    ReferenceSeed, ReferenceSeedCase, ReferenceSeedManifest, SELECTOR_SHA256,
)
from project_mai_tai.atr_simulation.firstbar_study import (
    StudyTapePacket, parse_study_tape, reconstruct_study_cases,
)
from project_mai_tai.atr_simulation.types import Coverage, EvidenceKind, SourceRef
from tests.support.atr_simulation_eligibility import POLICY


BASE = 1791792000  # CONTROLLED 2026-10-12 04:00 ET.
NS = 1_000_000_000


def source(name, raw):
    return SourceRef("CONTROLLED:" + name, hashlib.sha256(raw).hexdigest(), "$", EvidenceKind.CONTROLLED)


def inputs(*, accounts=("one",), omit_minute=None, unknown_minute=None):
    buy = "".join(f"{account}|schwab_1m_v2-X-open-{account}|limit|X|{BASE+604}|{BASE+605}|2.10|100||2.10\n"
                  for account in accounts).encode()
    buy_source = source("buys.psv", buy)
    lines = []
    for minute in range(21):
        if minute == omit_minute:
            continue
        condition = "malformed" if minute == unknown_minute else "14"
        lines.append(f"{BASE+minute*60}|{Decimal('2')+Decimal(minute)/100}|100|{{{condition}}}\n")
    tape = "".join(lines).encode()
    tape_source = source("X_2026-10-12.t", tape)
    coverage = Coverage(BASE * NS, (BASE + 16 * 3600) * NS, True, (), (tape_source,))
    packet = StudyTapePacket("X", "2026-10-12", tape, tape_source, coverage)
    selector = SourceRef("four/agg4.py", SELECTOR_SHA256, "lines4-6", EvidenceKind.RETAINED)
    manifest = ReferenceSeedManifest("CONTROLLED;NOT173", 1, buy_source, tape_source, selector)
    case = ReferenceSeedCase(("X", "2026-10-12", "04:10:05"), buy_source, tape_source,
                             (("qty", str(100 * len(accounts))),), ())
    seed = ReferenceSeed(manifest, (case,), (), Decimal(0), Decimal(0))
    return seed, buy, buy_source, packet


def reconstruct(data, accounts=("one",)):
    seed, buy, buy_source, packet = data
    return reconstruct_study_cases(seed, buy, buy_source, (packet,), execution_policy=POLICY,
        account_brokers={account: ("webull" if account == "one" else "schwab") for account in accounts},
        strategy_code="schwab_1m_v2", live_by_order={})


def test_connected_reconstruction_preserves_account_legs_and_completed_proxy_not_native_flip():
    result, = reconstruct(inputs(accounts=("one", "two")), accounts=("one", "two"))
    assert not result.issues
    assert len(result.legs) == len(result.inputs) == 2
    assert {item.episode.identity.account_id for item in result.inputs} == {"one", "two"}
    assert {item.episode.identity.broker for item in result.inputs} == {"webull", "schwab"}
    for item in result.inputs:
        assert item.episode.quantity == 100
        assert item.episode.identity.flip_id.startswith("REFERENCE_FILL_MINUTE:")
        assert item.episode.buy_flip.available_ns == (BASE + 660) * NS
        assert item.flip_bar.high == Decimal("2.10")
        assert item.flip_bar.complete is True  # Explicit CONTROLLED envelope only.
        assert item.episode.session_end.available_ns == (BASE + 16 * 3600) * NS
        assert item.episode.live.bought is None
        assert item.episode.watched_source.kind is EvidenceKind.DERIVED


def test_empty_proxy_minute_is_not_flat_filled_or_borrowed_from_next_bar():
    result, = reconstruct(inputs(omit_minute=10))
    assert result.inputs == ()
    assert result.issues == ("UNKNOWN_CAUSAL_PROXY_FLIP_BAR",)


def test_unknown_condition_prefix_not_silently_discarded_for_oracle():
    result, = reconstruct(inputs(unknown_minute=0))
    assert result.inputs == ()
    assert "UNKNOWN_CAUSAL_BAR_CONDITION_ELIGIBILITY" in result.issues


def test_unknown_envelope_is_not_promoted_by_raw_tape_presence():
    seed, buy, buy_source, packet = inputs()
    packet = replace(packet, coverage=replace(packet.coverage, complete=None))
    result, = reconstruct((seed, buy, buy_source, packet))
    assert result.inputs[0].flip_bar.complete is None
    assert result.inputs[0].episode.tape_coverage.complete is None


def test_same_millisecond_rows_retain_source_serialization_not_native_id_claim():
    _, _, _, packet = inputs()
    raw = f"{BASE}.123|2|100|{{14}}\n{BASE}.123|2.01|100|{{14}}\n".encode()
    packet = replace(packet, raw=raw, source=source("ties", raw))
    rows = parse_study_tape(packet, POLICY)
    assert rows[0].key.event_ns == rows[1].key.event_ns
    assert [row.key.sequence for row in rows] == [1, 2]
    assert all(row.key.raw_id.startswith("REFERENCE_TAPE:") for row in rows)
    assert rows[0].key.raw_id != rows[1].key.raw_id

