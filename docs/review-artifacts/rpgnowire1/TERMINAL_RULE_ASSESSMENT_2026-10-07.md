# RPGNOWIRE1 terminal-phase follow-up assessment

AGREE, before source edits. Published head is
5185d6b8d01ec51f3ae2bb39cfdfbad416d19e97; same sole-writer branch and PR #1115.
GitHub issue comments, reviews and inline comments were empty at this read;
the user's latest supplied reviewer condition is the review source:
terminal phase AND positive no-wire proof => release. Nonterminal no-wire
continues through the existing safe hand-off protocol.

Own code read: _rpg_release_unwired currently admits clear, held_unknown and
price_wait before _rpg_advance runs the coordinator. That bypasses the active
protocol. Conversely it excludes refused/expired, the two terminal-zero phases
recognized by rpg_buy_owned and v2's existing authorization lane. The narrow
follow-up will admit only refused/expired and preserve every positive-intent,
whole-generation/client/Fill veto, revision CAS and exact retained queue fence.
Filled, placed, dispatch-unknown and all other phases are not terminal-zero
release candidates. A completed feedback release is not repeatedly rewritten.

The own SXTC raw captures remain unchanged: primary held_unknown, mirror
price_wait at the recorded capture. Tests must label the terminal refused/expired
variants as controlled phase transitions, not claim those phases were recorded.
Both raw nonterminal cases must assert no new forced release, unchanged saved
request and continuing existing safe protocol. Terminal variants must assert
real serial feedback, matching per-account latch release and next ordinary draft;
no saved BUY in that terminal-release path. Neither age/reads=0/absence nor a
local_no_wire label alone proves a terminal phase or positive no-wire evidence.

Legacy active no-wire expectations will return to the existing protocol instead
of blanket accepting refused/clear. Genuine wired and filled controls stay intact.
No additional production pulls are needed: this is a code-policy narrowing using
the already captured exact requests/intents/retained row. No production action,
new flag, merge, rebase, amend, force-push or HOTFIX-owned file edit.
