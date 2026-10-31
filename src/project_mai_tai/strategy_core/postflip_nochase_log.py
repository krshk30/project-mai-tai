"""Observational formatting only; never calls or substitutes an admission guard.

The caller supplies the *already evaluated* refusal and an observed BUY-flip
bar identity. Unknown flip identity produces no affirmative no-chase claim.
One instance belongs to one symbol for its lifetime, not to a broker leg.
"""

from dataclasses import dataclass


@dataclass
class PostFlipNoChaseLog:
    symbol: str
    last_logged_flip_bar_ms: int = 0
    # Observed indicator identity only. Never derived from a retry ID or clock.
    current: tuple[int, int] = (0, 0)

    def reset_indicator(self):
        self.current = (0, 0)

    def observe_transition(self, *, flip, bar_ms, anchor_ms):
        if (type(bar_ms) is not int or type(anchor_ms) is not int
                or not 0 < anchor_ms <= bar_ms):
            self.reset_indicator()
        elif flip == "BUY":
            self.current = (anchor_ms, bar_ms)
        elif flip == "SELL":
            self.current = (anchor_ms, 0)

    def observed_line(self, *, anchor_ms, **operands):
        if type(anchor_ms) is not int or anchor_ms != self.current[0]:
            return None
        return self.line(flip_bar_ms=self.current[1], **operands)

    def line(self, *, allowed, refusal_reason, atr_state, short_segment,
             retry_segment, flip_bar_ms):
        """Return an additive INFO message or None; trading state is untouched.

        Exact integers/booleans prevent UNKNOWN or malformed metadata becoming
        an affirmative classification. Older history replays cannot re-emit an
        already reported flip. No clock, I/O, log delivery or account inference.
        """
        if (allowed is not False or refusal_reason != "retry_segment_mismatch"
                or atr_state != "long" or type(short_segment) is not int
                or short_segment != 0 or type(retry_segment) is not int
                or retry_segment <= 0 or type(flip_bar_ms) is not int
                or flip_bar_ms <= retry_segment
                or flip_bar_ms <= self.last_logged_flip_bar_ms
                or not isinstance(self.symbol, str) or not self.symbol
                or any(not (c.isascii() and (c.isalnum() or c in ".-"))
                       for c in self.symbol)):
            return None
        self.last_logged_flip_bar_ms = flip_bar_ms
        return (f"[V2-POSTFLIP-NOCHASE] sym={self.symbol} "
                f"flip_bar={flip_bar_ms} reason=already_flipped_not_chasing "
                "next=waiting_for_sell_flip")
