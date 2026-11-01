"""Run isolated offline guard mutations without changing worktree files."""
import argparse
import importlib.util
import json
from pathlib import Path
import types

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "scripts/wbquiet_history_replay.py"
TESTS = ROOT / "tests/unit/test_wbquiet_history_replay.py"


def run():
    spec = importlib.util.spec_from_file_location("controls", TESTS)
    tests = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tests)
    source = SOURCE.read_text()
    mutations = [
        ("held cadence reduced", 'return "held", 15', 'return "flat", 60',
         "test_hot_state_never_reduces_fifteen_second_cadence", ("held", "held")),
        ("terminal account match removed",
         'if any(row.get(key) != decision[key] for key in ("account", "client_order_id", "symbol", "side")):',
         'if any(row.get(key) != decision[key] for key in ("client_order_id", "symbol", "side")):',
         "test_terminal_match_needs_each_identity_freshness_and_source_guard", ({"account": "foreign"},)),
        ("accepted row treated terminal", 'if str(row.get("status", "")).lower() not in TERMINAL:', 'if False:',
         "test_terminal_match_needs_each_identity_freshness_and_source_guard", ({"status": "accepted"},)),
        ("reader window unbounded", 'if elapsed <= 60:', 'if True:',
         "test_reader_window_sixty_included_sixtyone_excluded", ()),
        ("counter final selection removed",
         'if (1 - row["partial"], row["total"]) > (1 - old["partial"], old["total"]):', 'if False:',
         "test_partial_endpoint_snapshots_not_summed_as_requests", ()),
        ("terminal freshness removed",
         'if not 0 <= age < 2 or not math.isfinite(filled) or filled < 0:',
         'if not math.isfinite(filled) or filled < 0:',
         "test_terminal_match_needs_each_identity_freshness_and_source_guard",
         ({"acquired_at": (tests.AT - tests.timedelta(seconds=2)).isoformat()},)),
        ("complete ET day falsely claimed", '"complete_ET_day_claim": False,', '"complete_ET_day_claim": True,',
         "test_missing_rotated_interval_is_enumerated_not_complete_day_claim", ()),
        ("reader overlap nearest pass adopted", 'elif len(anchors) > 1:', 'elif False:',
         "test_multiple_overlap_never_picks_nearest_or_claims_reuse", ()),
        ("committed generation identity removed", 'committed.get(field) != anchor.get(field)', 'False',
         "test_followup_anchor_each_identity_publication_window_guard", ({"local_read_id": "foreign"},)),
        ("reader loss ignored", 'if gap or dropped != 0:', 'if False:',
         "test_reader_reported_loss_vetoes_same_generation", ()),
        ("unknown equals unknown treated as cache match",
         'elif acquired is None or acquired != cache_identity(anchor.get("acquisition_generation")):',
         'elif acquired != cache_identity(anchor.get("acquisition_generation")):',
         "test_both_unknown_cache_ids_not_positive_same_generation", ()),
        ("reader anchor window unbounded", 'math.isfinite(elapsed) and 0 <= elapsed <= 60', 'math.isfinite(elapsed)',
         "test_followup_anchor_each_identity_publication_window_guard", ({"elapsed_seconds": 61},)),
        ("unpublished anchor accepted", 'anchor.get("shadow_receipt_emitted") is not True', 'False',
         "test_followup_anchor_each_identity_publication_window_guard", ({"shadow_receipt_emitted": False},)),
    ]
    receipts = []
    for name, old, new, test, args in mutations:
        if source.count(old) != 1:
            raise RuntimeError("mutation target not unique: " + name)
        module = types.ModuleType("wbquiet_mutation")
        exec(compile(source.replace(old, new), str(SOURCE), "exec"), module.__dict__)
        tests.history = module
        try:
            getattr(tests, test)(*args)
        except AssertionError:
            verdict = "RED"
        else:
            verdict = "SURVIVED"
        receipts.append({"mutation": name, "test": test, "verdict": verdict})
    return receipts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    receipts = run()
    with args.output.open("x") as stream:
        stream.write(json.dumps(receipts, indent=2) + "\n")
    print(json.dumps(receipts))
    raise SystemExit(any(row["verdict"] != "RED" for row in receipts))
