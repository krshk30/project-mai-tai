# F1/F3 Coverage Review Follow-up

- Source: main independent review of frozen39fc/200 controls; two MUST-ASSESS findings, queue amended before work.
- Priority1; same Anscombe sole F1/F3 lane. Parent39fc287e0f1051db0682df8fa245450bef949250 and prior evidence preserved.
- Status BOTH_FINDINGS_REPAIRED_LOCAL_212_CONTROLS; MAIN_SOURCE_ACCEPTANCE_AND_ACTUAL_F3_PENDING.
- F1: native OmsLogMonitor returned ambiguous complete_since_baseline even after a byte gap. New native receipt must distinguish consecutive observed end sequence from byte-gap coverage; strict independent sequence/provenance/source-age remains. Legacy controlled-input compatibility must not admit gap metadata with the old continuous string.
- F3/source: cumulative v2 gap count improperly tainted future fully observed due minutes. Restart actual covered interval at post-gap EOF observation; old retained true completion may be used, missing completion within a gap remains UNKNOWN; later fully observed due minute gets unchanged0-5s grace and >5s revert.
- Scope two health-source modules and focused controls/docs only; no box/performance action or unrelated policy changes. Actual F3 remains main-only after F2, target16:30-19:00ET, no exception not healthy; final composition/install main-owned.
