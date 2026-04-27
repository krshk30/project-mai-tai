from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from project_mai_tai.ai_trade_coach.models import TradeCoachConfig
from project_mai_tai.ai_trade_coach.models import TradeCoachReview
from project_mai_tai.ai_trade_coach.models import TradeEpisode
from project_mai_tai.ai_trade_coach.repository import TradeCoachRepository


logger = logging.getLogger(__name__)

ALLOWED_VERDICTS = {"good", "bad", "mixed", "skip"}
ALLOWED_ACTIONS = {"enter", "enter_early", "wait", "skip", "reduce", "exit", "hold"}
ALLOWED_EXECUTION_TIMINGS = {"early", "on_time", "late", "skip"}


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class TradeCoachClient:
    TOOL_NAME = "submit_trade_review"
    REVIEW_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": sorted(ALLOWED_VERDICTS)},
            "action": {"type": "string", "enum": sorted(ALLOWED_ACTIONS)},
            "execution_timing": {"type": "string", "enum": sorted(ALLOWED_EXECUTION_TIMINGS)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "setup_quality": {"type": "number", "minimum": 0, "maximum": 1},
            "should_have_traded": {"type": "boolean"},
            "key_reasons": {"type": "array", "items": {"type": "string"}},
            "rule_hits": {"type": "array", "items": {"type": "string"}},
            "rule_violations": {"type": "array", "items": {"type": "string"}},
            "next_time": {"type": "array", "items": {"type": "string"}},
            "concise_summary": {"type": "string"},
        },
        "required": [
            "verdict",
            "action",
            "execution_timing",
            "confidence",
            "setup_quality",
            "should_have_traded",
            "key_reasons",
            "rule_hits",
            "rule_violations",
            "next_time",
            "concise_summary",
        ],
        "additionalProperties": False,
    }

    def __init__(self, *, api_key: str, config: TradeCoachConfig | None = None) -> None:
        self.api_key = api_key.strip()
        self.config = config or TradeCoachConfig()

    def review_episode(self, *, rulebook: dict[str, Any], episode: TradeEpisode) -> TradeCoachReview:
        payload = {
            "model": self.config.model,
            "parallel_tool_calls": False,
            "tool_choice": {
                "type": "function",
                "name": self.TOOL_NAME,
            },
            "input": [
                {
                    "role": "system",
                    "content": (
                        "You are an expert trade review coach for a deterministic momentum trading engine. "
                        "Always call the submit_trade_review function exactly once. "
                        "Judge the trade against the provided rulebook and the captured episode context. "
                        "Use only the allowed enum values in the function schema. "
                        "Return confidence and setup_quality as decimals between 0.0 and 1.0. "
                        "Do not confuse outcome with quality: a losing trade is not automatically bad and a winning trade is not automatically good. "
                        "Avoid generic praise. Every key_reasons, rule_violations, and next_time item must point to concrete facts from the episode such as path, timing, scale behavior, stop behavior, bar context, or risk handling. "
                        "If the evidence is mixed, use the mixed verdict instead of defaulting to good. "
                        "Do not invent facts and do not output prose outside the function call."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "rulebook": rulebook,
                            "episode": episode.model_dump(mode="json"),
                        },
                        default=_json_default,
                    ),
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": self.TOOL_NAME,
                    "description": "Persisted review payload for a completed Mai Tai trade cycle.",
                    "parameters": self.REVIEW_SCHEMA,
                    "strict": True,
                }
            ],
        }
        data = self._request(payload)
        return TradeCoachReview.model_validate(self._normalize_review_payload(data))

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{self.config.base_url.rstrip('/')}/responses",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        request.add_header("Authorization", f"Bearer {self.api_key}")
        request.add_header("Content-Type", "application/json")

        try:
            with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                body = json.loads(response.read())
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:500]
            raise RuntimeError(f"Trade coach HTTP error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError("Trade coach network error") from exc

        raw_json = self._extract_response_json(body)
        try:
            return json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Trade coach returned invalid JSON: {raw_json[:500]}") from exc

    def _extract_response_json(self, body: dict[str, Any]) -> str:
        for item in body.get("output", []):
            if item.get("type") == "function_call" and item.get("name") == self.TOOL_NAME:
                arguments = item.get("arguments")
                if isinstance(arguments, str) and arguments.strip():
                    return arguments
            function_payload = item.get("function")
            if (
                item.get("type") == "function_call"
                and isinstance(function_payload, dict)
                and function_payload.get("name") == self.TOOL_NAME
            ):
                arguments = function_payload.get("arguments")
                if isinstance(arguments, str) and arguments.strip():
                    return arguments

        texts: list[str] = []
        for item in body.get("output", []):
            if item.get("type") != "message":
                continue
            for content_item in item.get("content", []):
                if content_item.get("type") == "output_text":
                    text = content_item.get("text")
                    if isinstance(text, str):
                        texts.append(text)
        if texts:
            return "\n".join(texts)
        raise RuntimeError("Trade coach response contained no review payload")

    def _normalize_review_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        normalized["verdict"] = self._normalize_verdict(payload.get("verdict"))
        normalized["action"] = self._normalize_action(payload.get("action"))
        normalized["execution_timing"] = self._normalize_execution_timing(payload.get("execution_timing"))
        normalized["confidence"] = self._normalize_score(payload.get("confidence"))
        normalized["setup_quality"] = self._normalize_score(payload.get("setup_quality"))
        normalized["should_have_traded"] = bool(payload.get("should_have_traded", False))
        for field in ("key_reasons", "rule_hits", "rule_violations", "next_time"):
            value = payload.get(field, [])
            if isinstance(value, list):
                normalized[field] = [str(item).strip() for item in value if str(item).strip()]
            elif value in (None, ""):
                normalized[field] = []
            else:
                normalized[field] = [str(value).strip()]
        normalized["concise_summary"] = str(payload.get("concise_summary", "") or "").strip()
        return normalized

    @staticmethod
    def _normalize_score(value: Any) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return 0.0
        if score > 1.0 and score <= 10.0:
            score = score / 10.0
        return max(0.0, min(score, 1.0))

    @staticmethod
    def _normalize_verdict(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in ALLOWED_VERDICTS:
            return text
        if any(token in text for token in ("win", "profit", "profitable", "solid", "good")):
            return "good"
        if any(token in text for token in ("loss", "loser", "bad", "poor", "failed")):
            return "bad"
        if "skip" in text or "no trade" in text:
            return "skip"
        return "mixed"

    @staticmethod
    def _normalize_action(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in ALLOWED_ACTIONS:
            return text
        if "enter early" in text or "early entry" in text:
            return "enter_early"
        if "enter" in text:
            return "enter"
        if "wait" in text:
            return "wait"
        if "skip" in text or "no trade" in text:
            return "skip"
        if "reduce" in text or "smaller" in text or "trim" in text:
            return "reduce"
        if any(token in text for token in ("exit", "close", "profit", "stop")):
            return "exit"
        if "hold" in text:
            return "hold"
        return "wait"

    @staticmethod
    def _normalize_execution_timing(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in ALLOWED_EXECUTION_TIMINGS:
            return text
        if "early" in text:
            return "early"
        if "late" in text:
            return "late"
        if "skip" in text or "no trade" in text:
            return "skip"
        if any(token in text for token in ("on time", "timely", "according to plan", "as planned")):
            return "on_time"
        return "on_time"


class TradeCoachService:
    def __init__(
        self,
        *,
        repository: TradeCoachRepository,
        coach_client: TradeCoachClient,
        rulebook: dict[str, Any],
        review_limit: int,
    ) -> None:
        self.repository = repository
        self.coach_client = coach_client
        self.rulebook = rulebook
        self.review_limit = review_limit

    async def run_review_cycle(
        self,
        *,
        strategy_accounts: list[tuple[str, str]],
        session_start,
        session_end,
    ) -> int:
        reviewed = 0
        cycles = self.repository.list_reviewable_cycles(
            strategy_accounts=strategy_accounts,
            session_start=session_start,
            session_end=session_end,
            review_limit=self.review_limit,
        )
        for cycle in cycles:
            episode = self.repository.build_episode(cycle=cycle)
            review = await asyncio.to_thread(
                self.coach_client.review_episode,
                rulebook=self.rulebook,
                episode=episode,
            )
            self.repository.save_review(
                cycle=cycle,
                review_payload=review.model_dump(mode="json"),
                provider=self.coach_client.config.provider,
                model=self.coach_client.config.model,
                primary_intent_id=episode.primary_intent_id,
            )
            reviewed += 1
        if reviewed:
            logger.info("trade coach reviewed %s completed trade cycles", reviewed)
        return reviewed
