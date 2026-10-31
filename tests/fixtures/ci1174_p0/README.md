# Bounded P0 regression inputs

`real_order_records.jsonl` retains six exact historical detail/list-today rows (3,981 bytes; SHA-256 `4451beda582a492a2371b84d07ffbf7b38107d8507f2a4c56b214988e69bbb1d`). These are not a retained nonempty list-open response or current native-flat proof. List envelopes and WORKING status changes in the tests are explicitly controlled.

`baseline_collector.txt` is the frozen pre-fix collector source (20,834 bytes; SHA-256 `37b94fbb3809fa5566a1c61b9d80604f83d1724e9b446392fc71b28715aa72a5`). The regression invokes only its pure `webull_pages` with controlled responses; it does not invoke native/provider collection. The text extension keeps immutable historical bytes separate from maintained Python modules. Tests verify both hashes before use. External environment paths, when explicitly supplied, remain subject to the same verification.
