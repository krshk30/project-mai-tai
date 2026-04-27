from __future__ import annotations

from project_mai_tai.ai_trade_coach.models import TradeCoachConfig
from project_mai_tai.ai_trade_coach.service import TradeCoachClient


def test_trade_coach_client_extracts_function_call_arguments() -> None:
    client = TradeCoachClient(api_key="test-key", config=TradeCoachConfig())

    payload = {
        "output": [
            {
                "type": "function_call",
                "name": "submit_trade_review",
                "arguments": '{"verdict":"good","action":"enter","execution_timing":"on_time","confidence":0.9,"setup_quality":0.8,"should_have_traded":true,"key_reasons":["trend"],"rule_hits":["P1"],"rule_violations":[],"next_time":["size up only with volume"],"concise_summary":"Solid entry."}',
            }
        ]
    }

    extracted = client._extract_response_json(payload)

    assert '"verdict":"good"' in extracted


def test_trade_coach_client_extracts_nested_function_arguments() -> None:
    client = TradeCoachClient(api_key="test-key", config=TradeCoachConfig())

    payload = {
        "output": [
            {
                "type": "function_call",
                "function": {
                    "name": "submit_trade_review",
                    "arguments": '{"verdict":"mixed","action":"wait","execution_timing":"late","confidence":0.5,"setup_quality":0.6,"should_have_traded":false,"key_reasons":["late extension"],"rule_hits":[],"rule_violations":["chased"],"next_time":["wait for reclaim"],"concise_summary":"Late chase."}',
                },
            }
        ]
    }

    extracted = client._extract_response_json(payload)

    assert '"verdict":"mixed"' in extracted


def test_trade_coach_client_normalizes_relaxed_model_payload() -> None:
    client = TradeCoachClient(api_key="test-key", config=TradeCoachConfig())

    normalized = client._normalize_review_payload(
        {
            "verdict": "win",
            "action": "closed position at profit",
            "execution_timing": "timely and according to plan",
            "confidence": 8,
            "setup_quality": 7,
            "should_have_traded": True,
            "key_reasons": ["strong momentum"],
            "rule_hits": [],
            "rule_violations": [],
            "next_time": ["same setup"],
            "concise_summary": "Solid trade.",
        }
    )

    assert normalized["verdict"] == "good"
    assert normalized["action"] == "exit"
    assert normalized["execution_timing"] == "on_time"
    assert normalized["confidence"] == 0.8
    assert normalized["setup_quality"] == 0.7
