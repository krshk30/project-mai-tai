"""Two NEW portable actual-marker controls; no callback/native replay claim."""

from collections import Counter
import hashlib
from pathlib import Path
import re


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/nochaselog1/pom_sprc_actual.tsv"
SHA = "3839690d7ab1610ac8e19b0185ce3851b7ddbbf1ee7fbf1a88c5692cc52577e9"
ADMISSION = "[V2-FLIP-OWNER-ADMISSION]"
COUNTRY = "[ATR-COUNTRY-REFUSED]"
ARM = "[V2-CW-ARM]"


def test_actual_sanitized_fixture_hash_and_exact_payload_free_shape():
    assert not FIXTURE.is_symlink()
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SHA
    assert len(raw) == 2984 and raw.endswith(b"\n")
    rows = raw.decode("ascii").splitlines()
    assert len(rows) == 57
    pattern = re.compile(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}\t(?:POM|SPRC)\t"
        r"\[(?:V2-FLIP-OWNER-ADMISSION|ATR-COUNTRY-REFUSED|V2-CW-ARM)\]")
    assert all(pattern.fullmatch(row) is not None for row in rows)
    # A strict three-column grammar admits no level, payload, account, key,
    # price, native/client ID or private-path data, even in an extra column.
    assert all(len(row.split("\t")) == 3 for row in rows)


def test_actual_pom_sprc_marker_counts_order_and_original_times_are_retained():
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SHA
    rows = [tuple(row.split("\t")) for row in raw.decode("ascii").splitlines()]
    counts = Counter((symbol, tag) for _, symbol, tag in rows)
    assert counts == {("POM", ARM): 1, ("POM", ADMISSION): 12,
                      ("POM", COUNTRY): 12, ("SPRC", ADMISSION): 16,
                      ("SPRC", COUNTRY): 16}
    pom = [row for row in rows if row[1] == "POM"]
    sprc = [row for row in rows if row[1] == "SPRC"]
    assert pom[0] == ("2026-10-28 14:52:02,755", "POM", ARM)
    assert pom[1] == ("2026-10-28 14:54:02,921", "POM", ADMISSION)
    assert pom[-1] == ("2026-10-28 15:05:04,364", "POM", COUNTRY)
    assert sprc[0] == ("2026-10-26 14:35:02,774", "SPRC", ADMISSION)
    assert sprc[-1] == ("2026-10-26 14:52:01,070", "SPRC", COUNTRY)
    for group in (pom[1:], sprc):
        for index in range(0, len(group), 2):
            admission, refusal = group[index:index + 2]
            assert admission[1] == refusal[1]
            assert admission[2] == ADMISSION and refusal[2] == COUNTRY
            assert admission[0] <= refusal[0]  # Preserve distinct original ms.
        assert [row[0] for row in group] == sorted(row[0] for row in group)
