"""Pure inputs for production-schema replay. No target, SQL or process IO.

Every source byte is covered, including pg_dump comments and psql restriction
directives. Client-only directives are preserved in evidence, never SQL-executed.
This parser is deliberately narrow: unknown syntax/control means STOP.
"""

from dataclasses import dataclass
from hashlib import sha256
import json
import re

VERSION = "L12_PRODUCTION_SCHEMA_REAL_ALEMBIC_REHEARSAL_V1"
C1_DELIVERY_VERSION = "L12_C11030_C1_DELIVERED_PRODUCTION_SCHEMA_V1"
START = "20261015_bot_control"
END = "20261022_l12_retirement"
MAX_SQL = 32 * 1024**2
MAX_RECORDS = 65536
CHAIN = (
    ("20261023_atr_simulation_store.py", "20261023_atr_simulation", START),
    ("20261023_atr_aggregate_entry.py", "20261023_atr_aggregate", "20261023_atr_simulation"),
    ("20261022_l12_dispatch_retirement.py", END, "20261023_atr_aggregate"),
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_rehearsal_input_key_STOP")
        value[key] = item
    return value


def is_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None and value != "0" * 64


def sha(raw):
    return sha256(raw).hexdigest()


def statement_kind(code):
    code = code.strip()
    setting = re.fullmatch(r"SET\s+(?:(?:SESSION|LOCAL)\s+)?([a-z_]+)\s*(?:=|TO)\s*.+;", code, re.I | re.S)
    if setting:
        if setting[1].lower() not in {"statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout",
                "client_encoding", "standard_conforming_strings", "check_function_bodies", "xmloption",
                "client_min_messages", "row_security", "default_tablespace", "default_table_access_method", "search_path"}:
            raise ValueError("unreviewed_schema_session_setting_STOP")
        if setting[1].lower() == "standard_conforming_strings" and not re.fullmatch(
                r"SET\s+standard_conforming_strings\s*(?:=|TO)\s*(?:on|'on');", code, re.I):
            raise ValueError("parser_requires_standard_conforming_strings_on_STOP")
        if setting[1].lower() == "client_encoding" and not re.fullmatch(
                r"SET\s+client_encoding\s*(?:=|TO)\s*'UTF8';", code, re.I):
            raise ValueError("parser_requires_UTF8_client_encoding_STOP")
        return "SET"
    if code == "SELECT pg_catalog.set_config('search_path', '', false);":
        return "SEARCH_PATH_CONTROL"
    if re.match(r"^(?:CREATE\s+(?:OR\s+REPLACE\s+)?|ALTER\s+)(?:TABLE|SEQUENCE|(?:UNIQUE\s+)?INDEX|"
            r"FUNCTION|PROCEDURE|SCHEMA|EXTENSION|TYPE|(?:MATERIALIZED\s+)?VIEW|(?:CONSTRAINT\s+)?TRIGGER|"
            r"RULE|POLICY|COLLATION|DOMAIN|CAST|CONVERSION|TEXT\s+SEARCH)\b", code, re.I):
        if (re.search(r"\bDISABLE\s+TRIGGER\b", code, re.I)
                or re.match(r"^CREATE\s+TABLE\b", code, re.I)
                and re.search(r"\bAS\s+(?:SELECT|WITH|TABLE)\b", code, re.I)):
            raise ValueError("schema_replay_data_or_disabled_trigger_path_STOP")
        return "DDL"
    if re.match(r"^(?:COMMENT\s+ON|SECURITY\s+LABEL\s+FOR)\b", code, re.I):
        return "ANNOTATION"
    raise ValueError("unsupported_schema_statement_STOP_no_favorable_omission")


@dataclass(frozen=True)
class Piece:
    kind: str
    raw: bytes
    start: int
    end: int

    def descriptor(self, ordinal):
        return dict(ordinal=ordinal, kind=self.kind, start=self.start, end=self.end, sha256=sha(self.raw))


def split_dump(raw):
    """Lossless SQL lexical boundaries, not a SQL semantic approval parser.

    Quoted identifiers, standard/E strings, dollar bodies and nested comments
    retain semicolons verbatim. Unknown psql commands and incomplete SQL refuse.
    """
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_SQL or not raw.endswith(b"\n") or b"\x00" in raw:
        raise ValueError("bounded_complete_UTF8_schema_dump_required")
    text = raw.decode("utf-8", errors="strict")
    pieces, start, index, state, depth, dollar, escape, code, active = [], 0, 0, "normal", 0, None, False, [], False
    restriction, unrestriction = None, False

    def emit(kind, stop):
        nonlocal start
        # Byte offsets differ from character offsets for non-ASCII identifiers.
        before = pieces[-1].end if pieces else 0
        content = text[start:stop].encode("utf-8")
        pieces.append(Piece(kind, content, before, before + len(content)))
        if len(pieces) > MAX_RECORDS:
            raise ValueError("schema_statement_count_bound_STOP")
        start = stop

    while index < len(text):
        char, following = text[index], text[index:index + 2]
        if state == "line":
            if char == "\n":
                state = "normal"
                code.append(" ")
        elif state == "block":
            if following == "/*":
                depth, index = depth + 1, index + 1
            elif following == "*/":
                depth, index = depth - 1, index + 1
                if depth == 0:
                    state = "normal"
                    code.append(" ")
        elif state == "single":
            code.append(char)
            if char == "\\" and escape:
                index += 1
                if index >= len(text):
                    raise ValueError("truncated_E_string_STOP")
                code.append(text[index])
            elif following == "''":
                code.append("'")
                index += 1
            elif char == "'":
                state = "normal"
        elif state == "double":
            code.append(char)
            if following == '""':
                code.append('"')
                index += 1
            elif char == '"':
                state = "normal"
        elif state == "dollar":
            if text.startswith(dollar, index):
                code.append(dollar)
                index += len(dollar) - 1
                state = "normal"
            else:
                code.append(char)
        elif following == "--":
            state, index = "line", index + 1
            code.append(" ")
        elif following == "/*":
            state, depth, index = "block", 1, index + 1
            code.append(" ")
        elif char == "\\":
            line_start = text.rfind("\n", 0, index) + 1
            if active or text[line_start:index].strip():
                raise ValueError("unexpected_psql_command_in_SQL_STOP")
            line_end = text.find("\n", index)
            if line_end < 0:
                raise ValueError("incomplete_psql_control_STOP")
            match = re.fullmatch(r"\\(restrict|unrestrict) ([A-Za-z0-9]+)", text[index:line_end])
            if match is None:
                raise ValueError("unknown_psql_command_STOP")
            if match[1] == "restrict":
                if restriction is not None or pieces:
                    raise ValueError("misplaced_repeated_restrict_STOP")
                restriction = match[2]
            elif restriction != match[2] or unrestriction:
                raise ValueError("mismatched_psql_restriction_STOP")
            else:
                unrestriction = True
            if start < line_start:
                emit("TRIVIA", line_start)
            emit("PSQL_" + match[1].upper(), line_end + 1)
            code, index = [], line_end
        elif char == "'":
            escape = index > 0 and text[index - 1] in "eE" and (index < 2 or not (text[index - 2].isalnum() or text[index - 2] == "_"))
            state, active = "single", True
            code.append(char)
        elif char == '"':
            state, active = "double", True
            code.append(char)
        elif char == "$" and (index == 0 or not (text[index - 1].isalnum() or text[index - 1] in "_$")):
            matched = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", text[index:])
            if matched:
                dollar, state, active = matched[0], "dollar", True
                code.append(dollar)
                index += len(dollar) - 1
            else:
                active = True
                code.append(char)
        elif char == ";":
            if not active or unrestriction:
                raise ValueError("empty_or_post_unrestrict_SQL_STOP")
            code.append(char)
            emit(statement_kind("".join(code)), index + 1)
            active, code = False, []
        else:
            if char.strip():
                if unrestriction:
                    raise ValueError("SQL_after_unrestrict_STOP")
                active = True
            code.append(char)
        index += 1
    if state not in {"normal", "line"} or active or restriction is not None and not unrestriction:
        raise ValueError("unterminated_schema_SQL_or_psql_control_STOP")
    if start < len(text):
        emit("TRIVIA", len(text))
    if b"".join(piece.raw for piece in pieces) != raw or not any(piece.kind == "DDL" for piece in pieces):
        raise ValueError("schema_byte_coverage_or_DDL_missing_STOP")
    return tuple(pieces)


@dataclass(frozen=True)
class ReviewedSchema:
    raw: bytes
    review_raw: bytes
    review_sha256: str
    pieces: tuple = ()
    delivery_artifacts: tuple = ()

    def __post_init__(self):
        if (not is_sha(self.review_sha256) or type(self.review_raw) is not bytes
                or not 0 < len(self.review_raw) <= 16 * 1024**2 or sha(self.review_raw) != self.review_sha256):
            raise ValueError("actual_independent_whole_DDL_review_required")
        review = json.loads(self.review_raw, object_pairs_hook=unique)
        common = {"purpose", "version", "schema_sha256", "authority_sha256", "alembic_revision",
            "whole_database_schema", "statements", "relations", "execution_policy", "review_id"}
        if not isinstance(review, dict):
            raise ValueError("exact_schema_provenance_review_required")
        if review.get("version") == C1_DELIVERY_VERSION:
            if (set(review) != common | {"delivery_sha256"}
                    or review["purpose"] != "MAIN_REVIEWED_C11030_C1_DELIVERED_PRODUCTION_DDL_ONLY"
                    or type(self.delivery_artifacts) is not tuple or len(self.delivery_artifacts) != 2
                    or any(type(value) is not bytes for value in self.delivery_artifacts)):
                raise ValueError("exact_C11030_raw_dump_README_revision_provenance_required")
            readme, revision = self.delivery_artifacts
            if (not 0 < len(readme) <= 65536 or not 0 < len(revision) <= 128
                    or revision.strip() != START.encode()
                    or review["delivery_sha256"] != {
                        "c1_prod_schema_20261024.sql": sha(self.raw),
                        "README.txt": sha(readme), "alembic_version.txt": sha(revision)}):
                raise ValueError("actual_C1_delivery_artifact_bytes_or_revision_changed")
        else:
            if (set(review) != common | {"dump_frames_sha256", "exported_snapshot"}
                    or review["purpose"] != "MAIN_REVIEWED_C10409_PRODUCTION_DDL_ONLY"
                    or review["version"] != VERSION or self.delivery_artifacts != ()
                    or not is_sha(review["dump_frames_sha256"])
                    or not isinstance(review["exported_snapshot"], str)
                    or not re.fullmatch(r"[0-9A-Fa-f]+-[0-9A-Fa-f]+-\d+", review["exported_snapshot"])):
                raise ValueError("explicit_same_snapshot_schema_review_and_strict_execution_policy_required")
        if (review["schema_sha256"] != sha(self.raw) or not is_sha(review["authority_sha256"])
                or review["alembic_revision"] != START or review["whole_database_schema"] is not True
                or review["execution_policy"] != "RAW_SQL_EACH_STATEMENT_750MS_250MS_NO_PARAMETERS"
                or not isinstance(review["review_id"], str) or not review["review_id"].strip()):
            raise ValueError("explicit_same_snapshot_schema_review_and_strict_execution_policy_required")
        pieces = split_dump(self.raw)
        if review["statements"] != [piece.descriptor(index) for index, piece in enumerate(pieces)]:
            raise ValueError("every_raw_statement_and_client_control_requires_exact_review")
        relations = review["relations"]
        if (not isinstance(relations, list) or not 21 <= len(relations) <= 4096
                or any(not isinstance(row, dict) or set(row) != {"schema", "name", "kind"}
                    or not all(isinstance(row[key], str) and row[key] for key in ("schema", "name"))
                    or row["kind"] not in {"r", "p", "v", "m", "S", "f"} for row in relations)
                or len({(row["schema"], row["name"]) for row in relations}) != len(relations)):
            raise ValueError("complete_whole_schema_relation_inventory_review_required")
        object.__setattr__(self, "pieces", pieces)

    @property
    def review(self):
        # Fresh decoded value prevents callers mutating the sealed review.
        return json.loads(self.review_raw, object_pairs_hook=unique)

    @property
    def provenance(self):
        value = self.review
        if value["version"] == C1_DELIVERY_VERSION:
            return dict(version=C1_DELIVERY_VERSION, delivery_sha256=value["delivery_sha256"],
                authority_sha256=value["authority_sha256"], alembic_revision=value["alembic_revision"],
                exported_snapshot_supplied=False, dump_frames_supplied=False)
        return dict(version=VERSION, dump_frames_sha256=value["dump_frames_sha256"],
            exported_snapshot=value["exported_snapshot"])

    @property
    def manifest_binding_sha256(self):
        # Existing lease slot binds actual provenance, never fabricated frames.
        value = self.review
        return (sha(canonical(self.provenance)) if value["version"] == C1_DELIVERY_VERSION
            else value["dump_frames_sha256"])
