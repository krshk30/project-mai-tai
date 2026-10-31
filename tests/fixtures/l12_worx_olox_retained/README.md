# Bounded retained L12 test inputs

The six `E_*` files and OLOX bars are exact retained bytes. Their SHA-256 checks remain in the existing E-packet manifest and replay loader. These inputs supply neither a complete session prefix nor historical native-flat proof.

`case_subset.json` contains only unchanged records `/103` (WORX) and `/110` (OLOX), extracted after validating the full 122-record source SHA-256 `31147bd20bb1f0c3e9a997bc45f9dbf41ef5fb71a6a58bec4b9c0a9e30c6c5f6`. Its own SHA-256 is `09958ee138421b6e91c987eefe1586a09aa59318fa20421e55b288d22510bb05`. It supports assertions about these two records, not the full export or missing native books. The module-local default validates both subset identity and hash; an explicit external export still uses the original full-export validation.

Current journals, broker responses and account bindings constructed by the tests are explicitly controlled inputs, not additions to these historical records.
