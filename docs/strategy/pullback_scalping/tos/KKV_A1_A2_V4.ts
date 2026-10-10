# ============================================================
# KKV A1 + A2 MOMENTUM SCALP - V4 DIAGNOSTIC
# (operator's thinkorswim study, saved verbatim 2026-10-07;
#  reading: docs/strategy/pullback_a1_a2_scalp_design.md)
#
# Designed for 1-minute small-cap / low-float momentum stocks
#
# A1 = 20/20 Momentum Continuation
# A2 = Impulse -> Pullback -> Reclaim
#
# GREEN ARROW = A1 BUY
# CYAN ARROW  = A2 BUY
#
# A2 QUALITY SCORE = 0 to 10
#
# IMPORTANT:
# New quality measurements are DIAGNOSTIC by default.
# Their hard-filter switches start OFF.
#
# ============================================================

declare upper;

# ============================================================
# DISPLAY
# ============================================================

input showLabels = yes;
input showDiagnostics = no;
input showBubbles = yes;

# OFF by default to avoid clutter.
input showA2SetupDots = no;

input showEMA9 = no;
input showEMA20 = no;

# ============================================================
# STOCHASTIC FUNCTION
# ============================================================

script StochD {
    input kLength = 9;
    input dLength = 3;
    input slowing = 1;

    def LL = Lowest(low, kLength);
    def HH = Highest(high, kLength);
    def R = HH - LL;

    def RawK = if R != 0 then 100 * (close - LL) / R else 50;
    def FullK = Average(RawK, slowing);
    plot D = Average(FullK, dLength);
}

# ============================================================
# STOCHASTICS
# ============================================================

def S9  = StochD(kLength = 9,  dLength = 3,  slowing = 1);
def S14 = StochD(kLength = 14, dLength = 3,  slowing = 1);
def S40 = StochD(kLength = 40, dLength = 4,  slowing = 1);
def S60 = StochD(kLength = 60, dLength = 10, slowing = 1);

# ============================================================
# EMA STRUCTURE
# ============================================================

input emaFastLength = 9;
input emaSlowLength = 20;

def EMA9  = ExpAverage(close, emaFastLength);
def EMA20 = ExpAverage(close, emaSlowLength);

plot EMA9Plot = if showEMA9 then EMA9 else Double.NaN;
EMA9Plot.SetDefaultColor(Color.CYAN);

plot EMA20Plot = if showEMA20 then EMA20 else Double.NaN;
EMA20Plot.SetDefaultColor(Color.YELLOW);

# ============================================================
# A1 - 20/20 CONTINUATION
# ============================================================

input A1SlowMinimum = 80.0;
input A1FastResetLevel = 25.0;
input A1MinimumFastRise = 2.0;
input A1ResetLookbackBars = 3;

input A1UseStrictCombined = no;
input A1Mid40Minimum = 60.0;
input A1Short14Minimum = 40.0;

def A1SlowOK  = S60 >= A1SlowMinimum;
def A1MidOK   = S40 >= A1Mid40Minimum;
def A1ShortOK = S14 >= A1Short14Minimum;

def A1RegimeOK =
    if A1UseStrictCombined
    then A1SlowOK and A1MidOK and A1ShortOK
    else A1SlowOK;

def A1RecentFastReset = Lowest(S9[1], A1ResetLookbackBars) <= A1FastResetLevel;

def A1FastTurn = S9 > S9[1] and (S9 - S9[1]) >= A1MinimumFastRise;

def A1Raw = A1RegimeOK and A1RecentFastReset and A1FastTurn;

def A1Buy = A1Raw and !A1Raw[1];

plot A1Arrow = if A1Buy then low - TickSize() * 6 else Double.NaN;
A1Arrow.SetPaintingStrategy(PaintingStrategy.ARROW_UP);
A1Arrow.SetDefaultColor(Color.GREEN);
A1Arrow.SetLineWeight(5);

AddChartBubble(showBubbles and A1Buy, low - TickSize() * 8, "A1 20/20", Color.GREEN, no);

# ============================================================
# A2 - IMPULSE -> PULLBACK -> RECLAIM
# ============================================================

# A2 IMPULSE
input A2ImpulseLookbackBars = 8;     # Short enough for scalping.
input A2ImpulseMinPct = 5.0;         # Keep broad enough to see opportunities.

def A2RecentHigh = Highest(high[1], A2ImpulseLookbackBars);
def A2RecentLow  = Lowest(low[1],  A2ImpulseLookbackBars);
def A2ImpulseRange = A2RecentHigh - A2RecentLow;

def A2ImpulsePct = if A2RecentLow > 0 then (A2ImpulseRange / A2RecentLow) * 100 else 0;
def A2ImpulseOK = A2ImpulsePct >= A2ImpulseMinPct;

# QUALITY 1 - FRESH IMPULSE HIGH
def A2BarsSinceHigh = GetMaxValueOffset(high[1], A2ImpulseLookbackBars) + 1;
input A2FreshHighMaxBars = 5;
def A2FreshHighQuality = A2BarsSinceHigh <= A2FreshHighMaxBars;

# PULLBACK BAR COUNT
# If high was 3 bars ago: HIGH, pullback bar 1, pullback bar 2, CURRENT trigger
# Therefore: pullback bars = bars since high - 1.
def A2PullbackBars = Max(A2BarsSinceHigh - 1, 0);

# QUALITY 8 - PULLBACK DURATION (2 bars = excellent, 3 bars = acceptable)
input A2PreferredPullbackMinBars = 2;
input A2PreferredPullbackMaxBars = 3;
def A2DurationQuality = A2PullbackBars >= A2PreferredPullbackMinBars and A2PullbackBars <= A2PreferredPullbackMaxBars;

# A2 ABSOLUTE PULLBACK DEPTH (still broad intentionally)
input A2PullbackMinPct = 1.5;
input A2PullbackMaxPct = 18.0;

def A2PullbackPct = if A2RecentHigh > 0 then ((A2RecentHigh - close[1]) / A2RecentHigh) * 100 else 0;
def A2PullbackDepthOK = A2PullbackPct >= A2PullbackMinPct and A2PullbackPct <= A2PullbackMaxPct;

# QUALITY 2 - RETRACEMENT FRACTION (10%-60% receives a quality point)
input A2RetraceMinFraction = 0.10;
input A2RetraceMaxFraction = 0.60;

def A2RetraceFraction = if A2ImpulseRange > 0 then (A2RecentHigh - close[1]) / A2ImpulseRange else 0;
def A2RetraceQuality = A2RetraceFraction >= A2RetraceMinFraction and A2RetraceFraction <= A2RetraceMaxFraction;

# QUALITY 3 - EMA STRUCTURE
input A2EMA20RiseLookbackBars = 3;
input A2EMA20HoldTolerancePct = 2.0;   # Deliberately generous for volatile stocks.

def A2EMAAlignment = EMA9 >= EMA20;
def A2EMA20Rising = EMA20 > EMA20[A2EMA20RiseLookbackBars];
def A2HoldsEMA20 = close[1] >= EMA20[1] * (1 - A2EMA20HoldTolerancePct / 100);
def A2EMAQuality = A2EMAAlignment and A2EMA20Rising and A2HoldsEMA20;

# PULLBACK VOLUME (2-bar average initially for responsiveness)
input A2PullbackVolumeBars = 2;
def A2PullbackVolume = Average(volume[1], A2PullbackVolumeBars);
def A2RecentVolumeAverage = Average(volume[1], 10);

# QUALITY 4 - RELATIVE VOLUME CONTRACTION
input A2PullbackVolumeMaxRatio = 0.80;
def A2PullbackVolumeRatio = if A2RecentVolumeAverage > 0 then A2PullbackVolume / A2RecentVolumeAverage else 0;
def A2RelativePullbackVolumeQuality = A2PullbackVolumeRatio <= A2PullbackVolumeMaxRatio;

# QUALITY 5 - ABSOLUTE PULLBACK VOLUME (low-float 1-minute liquidity floor; a testable hypothesis)
input A2MinPullbackVolumeAbs = 10000;
def A2AbsolutePullbackVolumeQuality = A2PullbackVolume >= A2MinPullbackVolumeAbs;

# QUALITY 9 - PULLBACK RANGE COMPRESSION
input A2PullbackRangeBars = 2;
input A2RangeCompressionMaxRatio = 0.85;

def A2PullbackRangeAverage = Average(high[1] - low[1], A2PullbackRangeBars);
def A2RecentRangeAverage = Average(high[1] - low[1], 8);
def A2RangeCompressionRatio = if A2RecentRangeAverage > 0 then A2PullbackRangeAverage / A2RecentRangeAverage else 0;
def A2RangeCompressionQuality = A2RangeCompressionRatio <= A2RangeCompressionMaxRatio;

# A2 BASIC TRIGGER BAR (permissive enough to produce candidates)
input A2MinTriggerBodyPct = 0.20;
input A2MinCloseLocation = 0.60;   # 0.60 = close in upper 40% of candle.

def A2GreenBar = close > open;
def A2BodyPct = if open > 0 then ((close - open) / open) * 100 else 0;
def A2BodyOK = A2BodyPct >= A2MinTriggerBodyPct;
def A2BarRange = high - low;
def A2CloseLocation = if A2BarRange > 0 then (close - low) / A2BarRange else 1;
def A2CloseQuality = A2CloseLocation >= A2MinCloseLocation;

# Base reclaim: prior-bar reclaim so signals do not disappear.
def A2ReclaimsPriorHigh = close > high[1];

def A2PriceTrigger = A2GreenBar and A2BodyOK and A2CloseQuality and A2ReclaimsPriorHigh;

# QUALITY 10 - 2/3 BAR PIVOT RECLAIM (start with 2 for fast scalping)
input A2PivotLookbackBars = 2;
def A2PullbackPivotHigh = Highest(high[1], A2PivotLookbackBars);
def A2PivotReclaimQuality = close > A2PullbackPivotHigh;

# TRIGGER VOLUME (modest to retain candidates)
input A2TriggerVolumeRatio = 1.10;
input A2MinTriggerVolume = 5000;
def A2TriggerVsPullbackVolume = if A2PullbackVolume > 0 then volume / A2PullbackVolume else 0;

# QUALITY 6 - TRIGGER VOLUME RETURNS
def A2TriggerVolumeQuality = volume >= A2MinTriggerVolume and A2TriggerVsPullbackVolume >= A2TriggerVolumeRatio;

# QUALITY 7 - ANTI-CHASE / EMA9 DISTANCE
input A2MaxEMA9DistancePct = 3.0;
def A2EMA9DistancePct = if EMA9 > 0 then ((close - EMA9) / EMA9) * 100 else 0;
def A2AntiChaseQuality = A2EMA9DistancePct <= A2MaxEMA9DistancePct;

# ============================================================
# OPTIONAL HARD FILTER SWITCHES (ALL START OFF)
# ============================================================

input A2UseFreshHighFilter = no;
input A2UseRetraceFilter = no;
input A2UseEMAFilter = no;
input A2UseRelativePullbackVolumeFilter = no;
input A2UseAbsolutePullbackVolumeFilter = no;
input A2UseTriggerVolumeFilter = no;
input A2UseAntiChaseFilter = no;
input A2UseDurationFilter = no;
input A2UseRangeCompressionFilter = no;
input A2UsePivotReclaimFilter = no;

def A2FreshFilterOK            = if A2UseFreshHighFilter              then A2FreshHighQuality              else yes;
def A2RetraceFilterOK          = if A2UseRetraceFilter                then A2RetraceQuality                else yes;
def A2EMAFilterOK              = if A2UseEMAFilter                    then A2EMAQuality                    else yes;
def A2RelativePBVolumeFilterOK = if A2UseRelativePullbackVolumeFilter then A2RelativePullbackVolumeQuality else yes;
def A2AbsolutePBVolumeFilterOK = if A2UseAbsolutePullbackVolumeFilter then A2AbsolutePullbackVolumeQuality else yes;
def A2TriggerVolumeFilterOK    = if A2UseTriggerVolumeFilter          then A2TriggerVolumeQuality          else yes;
def A2AntiChaseFilterOK        = if A2UseAntiChaseFilter              then A2AntiChaseQuality              else yes;
def A2DurationFilterOK         = if A2UseDurationFilter               then A2DurationQuality               else yes;
def A2RangeCompressionFilterOK = if A2UseRangeCompressionFilter       then A2RangeCompressionQuality       else yes;
def A2PivotReclaimFilterOK     = if A2UsePivotReclaimFilter           then A2PivotReclaimQuality           else yes;

# BASE A2 SETUP: significant impulse + actual pullback
def A2BaseSetup = A2ImpulseOK and A2PullbackDepthOK;
def A2SetupStart = A2BaseSetup and !A2BaseSetup[1];

# A2 QUALITY SCORE - 10 POINTS
def A2QualityScore =
    (if A2FreshHighQuality then 1 else 0) +
    (if A2RetraceQuality then 1 else 0) +
    (if A2EMAQuality then 1 else 0) +
    (if A2RelativePullbackVolumeQuality then 1 else 0) +
    (if A2AbsolutePullbackVolumeQuality then 1 else 0) +
    (if A2TriggerVolumeQuality then 1 else 0) +
    (if A2AntiChaseQuality then 1 else 0) +
    (if A2DurationQuality then 1 else 0) +
    (if A2RangeCompressionQuality then 1 else 0) +
    (if A2PivotReclaimQuality then 1 else 0);

# A2 SIGNAL BEFORE COOLDOWN
def A2SignalBeforeCooldown =
    A2BaseSetup and A2PriceTrigger
    and A2FreshFilterOK and A2RetraceFilterOK and A2EMAFilterOK
    and A2RelativePBVolumeFilterOK and A2AbsolutePBVolumeFilterOK
    and A2TriggerVolumeFilterOK and A2AntiChaseFilterOK
    and A2DurationFilterOK and A2RangeCompressionFilterOK and A2PivotReclaimFilterOK;

# A2 COOLDOWN (keep short)
input A2CooldownBars = 3;

rec A2CooldownCounter =
    CompoundValue(1,
        if A2SignalBeforeCooldown and A2CooldownCounter[1] == 0 then A2CooldownBars
        else if A2CooldownCounter[1] > 0 then A2CooldownCounter[1] - 1
        else 0,
        0);

def A2Buy = A2SignalBeforeCooldown and A2CooldownCounter[1] == 0 and !A1Buy;

# OPTIONAL SETUP DOT
plot A2SetupDot = if showA2SetupDots and A2SetupStart then low - TickSize() * 2 else Double.NaN;
A2SetupDot.SetPaintingStrategy(PaintingStrategy.POINTS);
A2SetupDot.SetDefaultColor(Color.ORANGE);
A2SetupDot.SetLineWeight(4);

# A2 BUY ARROW
plot A2Arrow = if A2Buy then low - TickSize() * 6 else Double.NaN;
A2Arrow.SetPaintingStrategy(PaintingStrategy.ARROW_UP);
A2Arrow.SetDefaultColor(Color.CYAN);
A2Arrow.SetLineWeight(5);

# A2 QUALITY BUBBLE
AddChartBubble(showBubbles and A2Buy, low - TickSize() * 8,
    if A2QualityScore >= 8 then "A2++ " + A2QualityScore + "/10"
    else if A2QualityScore >= 6 then "A2+ " + A2QualityScore + "/10"
    else "A2 " + A2QualityScore + "/10",
    if A2QualityScore >= 8 then Color.CYAN else if A2QualityScore >= 6 then Color.YELLOW else Color.GRAY,
    no);

# MAIN LABELS
AddLabel(showLabels,
    if A1Buy then "A1 BUY" else if A1RegimeOK then "A1 READY" else "A1 WAIT",
    if A1Buy then Color.GREEN else if A1RegimeOK then Color.GREEN else Color.GRAY);

AddLabel(showLabels,
    if A2Buy then "A2 BUY " + A2QualityScore + "/10"
    else if A2BaseSetup then "A2 SETUP " + A2QualityScore + "/10"
    else if !A2ImpulseOK then "A2 WAIT: IMPULSE"
    else "A2 WAIT: PULLBACK",
    if A2Buy then Color.CYAN else if A2BaseSetup then Color.ORANGE else Color.GRAY);

# DIAGNOSTIC LABELS
AddLabel(showDiagnostics, "PB BARS " + A2PullbackBars, if A2DurationQuality then Color.GREEN else Color.GRAY);
AddLabel(showDiagnostics, "IMP " + Round(A2ImpulsePct, 1) + "%", if A2ImpulseOK then Color.GREEN else Color.GRAY);
AddLabel(showDiagnostics, "PB " + Round(A2PullbackPct, 1) + "%", if A2PullbackDepthOK then Color.GREEN else Color.GRAY);
AddLabel(showDiagnostics, "RETRACE " + Round(A2RetraceFraction * 100, 0) + "%", if A2RetraceQuality then Color.GREEN else Color.RED);
AddLabel(showDiagnostics, "PB VOL " + Round(A2PullbackVolume, 0), if A2AbsolutePullbackVolumeQuality then Color.GREEN else Color.RED);
AddLabel(showDiagnostics, "PB REL " + Round(A2PullbackVolumeRatio, 2) + "x", if A2RelativePullbackVolumeQuality then Color.GREEN else Color.RED);
AddLabel(showDiagnostics, "TRIG VOL " + Round(A2TriggerVsPullbackVolume, 2) + "x", if A2TriggerVolumeQuality then Color.GREEN else Color.RED);
AddLabel(showDiagnostics, "RANGE " + Round(A2RangeCompressionRatio, 2) + "x", if A2RangeCompressionQuality then Color.GREEN else Color.RED);
AddLabel(showDiagnostics, "PIVOT " + (if A2PivotReclaimQuality then "PASS" else "FAIL"), if A2PivotReclaimQuality then Color.GREEN else Color.RED);
AddLabel(showDiagnostics, "EMA DIST " + Round(A2EMA9DistancePct, 1) + "%", if A2AntiChaseQuality then Color.GREEN else Color.RED);

# ALERTS
Alert(A1Buy, "KKV A1 BUY", Alert.BAR, Sound.Ding);
Alert(A2Buy, "KKV A2 BUY", Alert.BAR, Sound.Bell);
