"""Only the historical simulation fixture can accept its retired switches."""

from project_mai_tai.settings import Settings


class RetiredOrbSettings(Settings):
    orb_intrabar_reclaim_enabled: bool = False
    orb_running_high_enabled: bool = False
    orb_resting_entry_enabled: bool = False
    orb_paper_lifecycle_enabled: bool = False
    orb_paper_atr_exit_enabled: bool = True
    orb_paper_atr_entry_gate_enabled: bool = False
    orb_paper_four_red_delay_enabled: bool = False
