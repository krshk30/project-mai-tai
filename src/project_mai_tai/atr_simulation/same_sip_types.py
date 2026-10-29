"""Current TODAY input extension; legacy result types and seals stay original."""
from dataclasses import dataclass
from .types import *
from .types import EpisodeInput as LegacyEpisodeInput


@dataclass(frozen=True)
class EpisodeInput(LegacyEpisodeInput):
    same_sip_policy: str | None = None
