"""Anticipatory ATR-proximity entry with the TOS dot-plot consensus filter (R&D, 2026-07-21).

THE RULE (operator's, from his TSDotPlotV2 thinkScript + chart):
  Today's CW entry waits for the ATR trail to FLIP long, then 3 bars, then a break of the
  3-bar high -- i.e. it buys CONFIRMATION. This buys ANTICIPATION instead: while the trail is
  still SHORT (purple dots above price), enter when the bar CLOSES within X% of the trail AND
  the three-row dot consensus is green on this bar AND the prior bar.

PORTED VERBATIM from the operator's thinkScript -- every row is "turning up off a recent low
and out of the basement", an INFLECTION detector, not a level/trend check:

    MACD  : macd(6,13) > lowest(macd(6,13),3)[1]
    STOCH : (stochasticfast() > lowest(stochasticfast(),5)[1] and stochasticfast() > 30)
            or stochasticfast() > 70
    RSI   : (rsi() > lowest(rsi(),5)[1] and rsi() > 30) or rsi() > 70
    allGreen = consensus >= 3   (ALL three, per the operator)

thinkScript `lowest(x, n)[1]` = min over the n bars ENDING AT THE PRIOR BAR, i.e.
min(x[t-1] .. x[t-n]) -- NOT including the current bar. Getting this off by one silently
turns "turned up off its low" into "is its own low", so it is pinned in tests.

TOS DEFAULTS ASSUMED (declared, because they move the numbers):
    macd(6,13)        -> Value = EMA(close,6) - EMA(close,13)   (first plot, not the histogram)
    stochasticfast()  -> FastK, KPeriod=10                       (first plot)
    rsi()             -> length 14, Wilders

Stdlib only, matching the rest of the engine (no pandas).
"""

from __future__ import annotations

from dataclasses import dataclass

NAN = float("nan")


# ----------------------------------------------------------------- primitives


def ema(values: list[float], length: int) -> list[float]:
    """EMA seeded on the first value (TOS ExpAverage behaviour)."""
    out: list[float] = []
    k = 2.0 / (length + 1.0)
    prev = NAN
    for i, v in enumerate(values):
        if i == 0:
            prev = v
        else:
            prev = v * k + prev * (1.0 - k)
        out.append(prev)
    return out


def macd_value(closes: list[float], fast: int = 6, slow: int = 13) -> list[float]:
    f, s = ema(closes, fast), ema(closes, slow)
    return [f[i] - s[i] for i in range(len(closes))]


def fast_stoch_k(
    highs: list[float], lows: list[float], closes: list[float], k_period: int = 10
) -> list[float]:
    """TOS StochasticFast FullK = 100*(close-LL)/(HH-LL) over k_period."""
    out: list[float] = []
    for i in range(len(closes)):
        if i + 1 < k_period:
            out.append(NAN)
            continue
        window = slice(i - k_period + 1, i + 1)
        hh, ll = max(highs[window]), min(lows[window])
        rng = hh - ll
        out.append(50.0 if rng <= 0 else 100.0 * (closes[i] - ll) / rng)
    return out


def rsi_wilders(closes: list[float], length: int = 14) -> list[float]:
    """Wilders RSI (TOS default averageType)."""
    out: list[float] = [NAN] * len(closes)
    if len(closes) <= length:
        return out
    gains = losses = 0.0
    for i in range(1, length + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / length, losses / length
    out[length] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    for i in range(length + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (length - 1) + max(d, 0.0)) / length
        avg_l = (avg_l * (length - 1) + max(-d, 0.0)) / length
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def lowest_prior(series: list[float], n: int, i: int) -> float:
    """thinkScript lowest(series, n)[1] evaluated at bar i.

    = min(series[i-1] .. series[i-n]); NaN if the window isn't fully available or
    contains a NaN (an unwarmed indicator must not manufacture a green).
    """
    if i - n < 0:
        return NAN
    window = series[i - n : i]
    if any(v != v for v in window):
        return NAN
    return min(window)


# ----------------------------------------------------------------- the rows


def _green_macd(macd: list[float], i: int) -> bool:
    lo = lowest_prior(macd, 3, i)
    return lo == lo and macd[i] == macd[i] and macd[i] > lo


def _green_band(series: list[float], i: int) -> bool:
    """Shared STOCH/RSI shape: (up off the 5-bar low AND >30) OR >70."""
    v = series[i]
    if v != v:
        return False
    if v > 70.0:
        return True
    lo = lowest_prior(series, 5, i)
    return lo == lo and v > lo and v > 30.0


@dataclass(frozen=True)
class DotRows:
    macd: list[float]
    stoch: list[float]
    rsi: list[float]

    def consensus(self, i: int) -> int:
        return (
            int(_green_macd(self.macd, i))
            + int(_green_band(self.stoch, i))
            + int(_green_band(self.rsi, i))
        )

    def all_green(self, i: int) -> bool:
        return self.consensus(i) >= 3


def build_rows(
    highs: list[float], lows: list[float], closes: list[float]
) -> DotRows:
    return DotRows(
        macd=macd_value(closes),
        stoch=fast_stoch_k(highs, lows, closes),
        rsi=rsi_wilders(closes),
    )


# ------------------------------------------------- confirmation filters (2026-07-21)
#
# The operator described the dot plot two different ways, so BOTH are built and reported
# rather than one being guessed at:
#   - his thinkScript source      : MACD + StochasticFast + RSI   ("script")
#   - his verbal description      : MACD + StochK + VOLUME        ("volume")
# Volume appears nowhere in the script; the verbal version is what he actually asked for,
# so it leads. Whichever filters better is an empirical question, not a drafting one.
#
# Two strictnesses are run because "compared to the last three bars" is ambiguous and the
# choice moves entry counts a lot:
#   loose  : > lowest(x,3)[1]      -- turned up off the recent low (the script's own shape)
#   strict : > max(prev 3 bars)    -- a genuine 3-bar breakout in the indicator


def _above_prior_max(series: list[float], n: int, i: int) -> bool:
    if i - n < 0:
        return False
    w = series[i - n : i]
    if any(v != v for v in w) or series[i] != series[i]:
        return False
    return series[i] > max(w)


def make_filter(kind: str, rows: DotRows, volumes: list[float]):
    """Return f(i) -> bool for the signal bar index i. `kind` in:
    none | script | volume_loose | volume_strict
    """
    if kind == "none":
        return None

    if kind == "script":                     # exact thinkScript: all three rows green
        return lambda i: rows.all_green(i)

    vols = [float(v) for v in volumes]
    if kind == "volume_loose":
        return lambda i: (
            _green_macd(rows.macd, i)
            and _green_band(rows.stoch, i)
            and (lowest_prior(vols, 3, i) == lowest_prior(vols, 3, i)
                 and vols[i] > lowest_prior(vols, 3, i))
        )
    if kind == "volume_strict":
        return lambda i: (
            _above_prior_max(rows.macd, 3, i)
            and _above_prior_max(rows.stoch, 3, i)
            and _above_prior_max(vols, 3, i)
        )
    raise ValueError(f"unknown filter kind: {kind}")


FILTER_KINDS = ("none", "script", "volume_loose", "volume_strict")
