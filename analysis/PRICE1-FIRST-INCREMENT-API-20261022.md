# PRICE1 first source increment: partial / UNRUN

Base aa16266823e7ef0ebb58a44e8efb64af417fc2bc, new isolated PRICE1 branch.
Frozen L13/L6/other-lane worktrees untouched. Main owns runtime/queue/LATEST.

Shared helper buy_price_floor.buy_price_allowed(price, *, bot, symbol, side='buy')
reads only supplied price; BUY below1/unknown/nonfinite refuses+PRICE1-REFUSED
bot/symbol/price; SELL bypasses before any price parse. No fetch/DB/provider.

Latest14:24 amendment applied exactly: warning_consumer candidate filter uses
bar.close<1 instead of yesterday close, same IGNORED reason/cap; engine bucket
filter uses signal_price<1, keeping prior_close None refusal and all prior math.
No removal of prior-close data/percent math/scanner limits. Legacy paper raw
_can_detect path remains unchanged pending source-path review; not falsely
claimed fixed by this first increment.

Live executor _request checks signal and final priced limit with shared helper;
normal quote/age/cap/owner/segment logic unchanged. MOMNEG1 source belongs to
Cicero, NOT implemented here. Main coordination: touched execution.py _request
ONLY, engine.py detect_from_bucket ONE condition, warning_consumer.py ONE
condition. No Signal/admission-port API change; no runtime.py edit in increment.

New13 pure controls UNRUN. ATR/ORB decisions, waiting preservation, remaining
reprice/wire callgraph and built-path controls are next, not completed/qualified.
Candidate-count10-12 and actual combined PRICE1/MOMNEG1 replay require main/
Claude execution/inputs; no counts or native outcomes fabricated.
