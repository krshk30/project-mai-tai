"""Exact retained current tickets; injected book is not historical clearance."""
import importlib.util
from pathlib import Path


path = Path(__file__).parents[1] / "slotclear1/test_both_paths.py"
spec = importlib.util.spec_from_file_location("retained_slot_ticket_controls", path)
controls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controls)


def test_nfq_on_sell_slot_release_cannot_clear_restored_rpg_veto(monkeypatch):
    original = controls.seeded

    def joint_seeded(**kwargs):
        values = original(**kwargs)
        values[0].settings.oms_v2_eh_fresh_price_enabled = True
        return values

    monkeypatch.setattr(controls, "seeded", joint_seeded)
    controls.test_present_durable_sxtc_tickets_veto_entry_after_recorded_sell_even_handoff_off()


def test_nfq_on_fresh_buy_stays_isolated_from_restored_other_symbol_tickets(monkeypatch):
    original = controls.lpcn

    def joint_lpcn():
        values = original()
        values[0].settings.oms_v2_eh_fresh_price_enabled = True
        return values

    monkeypatch.setattr(controls, "lpcn", joint_lpcn)
    controls.test_recorded_lpcn_fresh_buy_isolated_from_present_sxtc_dki_tickets()
