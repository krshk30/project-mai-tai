"""Explicit successor pins; never refresh or bypass historical replay seals."""
import hashlib
from pathlib import Path
from .engine_binding import verify_engine_files
from .firstbar_binding import verify_firstbar_files
from .same_sip import SAME_SIP_CONSERVATIVE_V1

POLICY_VERSION = SAME_SIP_CONSERVATIVE_V1
SOURCE_PINS = {'same_sip.py': '508ac439bd419399f51312681f5fd5425821b11e9d23bb9604ad900e7e78878e', 'same_sip_types.py': 'fc052a6e2b163cdcaa79d0577301a26d3a83087b31ed7ce2eae3bbae5129230e', 'same_sip_engine.py': '932ae1f5858318b337e6fc4d7da47ae0dfa3bafbb98aeec45acad025aa9ce91b', 'same_sip_exits.py': '5c984fb6173b02fb944c041ec26356ff447f063a81dd1ed8822d24f743d752b1', 'same_sip_fill_models.py': 'fa46737ef5e8f177e3aa518f684d79970deafdc2c624d84b966c36665989c47c', 'same_sip_firstbar.py': '1207795ab467b96453acf490b044029bed78c69a68719b26a3729d55c9709023', 'same_sip_firstbar_exits.py': 'e22e1519582852ef7b2e3c4868d36433ed7466838515364b902243470ed7aeb6', 'same_sip_firstbar_replay.py': '8746a68aa3a58f377c59aa283eaefcf14937af7a9dc0460b1aa216ec9d5de47f', 'same_sip_firstbar_live_baseline.py': '6ac7e960e9fa35e69fef895030307f02aec0ba188b89c85426a1a784dfa16406'}
SOURCE_PINS['same_sip_reuse.py'] = 'ce404bf0d9c82a70421265c1bdb2cf656a6cc290268e8cccfc491d190c36e187'
SOURCE_PINS['same_sip_eligibility.py'] = 'c7afa5c2368b506155251a0dbcf1ac1ab90b13eccaa1e5f7dd2c50c2c55d2831'


def verify_same_sip_files(root=None):
    root = Path(root).resolve() if root is not None else Path(__file__).resolve().parent
    verify_engine_files(root)
    verify_firstbar_files(root)
    for name, expected in SOURCE_PINS.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError('same_sip_successor_source_pin_mismatch:' + name)
    return POLICY_VERSION
