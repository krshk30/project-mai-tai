"""Pure schema parsing/review controls. Controlled DDL is not actual production."""

from copy import deepcopy

import pytest

from tools import l12_schema_rehearsal_inputs as inputs


SQL = b"-- CONTROLLED not production\nSET statement_timeout = 0;\nCREATE TABLE public.controlled (id integer);\n"


def review(raw=SQL):
    return dict(purpose="MAIN_REVIEWED_C10409_PRODUCTION_DDL_ONLY", version=inputs.VERSION,
        schema_sha256=inputs.sha(raw), dump_frames_sha256="a" * 64, authority_sha256="b" * 64,
        alembic_revision="20261015_bot_control", exported_snapshot="00000001-00000002-1",
        whole_database_schema=True, statements=[piece.descriptor(index) for index, piece in enumerate(inputs.split_dump(raw))],
        relations=[dict(schema="public", name=f"controlled_{index}", kind="r") for index in range(21)],
        execution_policy="RAW_SQL_EACH_STATEMENT_750MS_250MS_NO_PARAMETERS", review_id="CONTROLLED_NOT_AUTHORITY")


@pytest.mark.parametrize("body", [
    "CREATE TABLE public.t (v text DEFAULT ';');\n",
    "CREATE TABLE public.t (\"semi;col\" text);\n",
    "CREATE FUNCTION public.f() RETURNS text LANGUAGE sql AS $$ SELECT ';'; $$;\n",
    "CREATE FUNCTION public.f() RETURNS text LANGUAGE sql AS $body$ SELECT ';'; $body$;\n",
    "/* OUTER /* INNER ; */ ; */ CREATE TABLE public.t (v integer); -- TRAILING ;\n",
    "CREATE TABLE public.t (v text DEFAULT E'back\\\\slash;quote\\\'x');\n",
    "CREATE TABLE public.\"caf\u00e9\" (v text DEFAULT 'a''b;');\n",
    "CREATE VIEW public.v AS SELECT 1;\n",
])
def test_lossless_lexical_boundaries_preserve_semicolons_and_UTF8_byte_offsets(body):
    raw = body.encode("utf-8")
    pieces = inputs.split_dump(raw)
    assert b"".join(piece.raw for piece in pieces) == raw
    assert sum(piece.kind == "DDL" for piece in pieces) == 1
    position = 0
    for piece in pieces:
        assert piece.start == position and raw[piece.start:piece.end] == piece.raw
        position = piece.end
    assert position == len(raw)


def test_psql_restriction_pair_is_preserved_not_SQL_executed():
    raw = b"-- HEADER\n\\restrict ABC123\nCREATE TABLE public.t (id integer);\n\\unrestrict ABC123\n-- EOF\n"
    pieces = inputs.split_dump(raw)
    assert b"".join(piece.raw for piece in pieces) == raw
    assert [piece.kind for piece in pieces] == ["TRIVIA", "PSQL_RESTRICT", "DDL", "TRIVIA", "PSQL_UNRESTRICT", "TRIVIA"]


@pytest.mark.parametrize("raw", [
    b"CREATE TABLE public.t (id integer)",
    b"CREATE TABLE public.t (v text DEFAULT 'unterminated);\n",
    b'CREATE TABLE public.t ("unterminated integer);\n',
    b"/* unterminated CREATE TABLE public.t (id integer);\n",
    b"CREATE FUNCTION public.f() RETURNS text AS $x$unterminated;\n",
    b"\\! echo forbidden\nCREATE TABLE public.t (id integer);\n",
    b"\\restrict A\nCREATE TABLE public.t (id integer);\n\\unrestrict B\n",
    b"\\restrict A\nCREATE TABLE public.t (id integer);\n",
    b"\\restrict A\nCREATE TABLE public.t (id integer);\n\\unrestrict A\nSET statement_timeout=0;\n",
    b"CREATE TABLE public.t (v text);\nCOPY public.t FROM stdin;\n",
    b"CREATE TABLE public.t (v text);\nINSERT INTO public.t VALUES ('row');\n",
    b"CREATE TABLE public.t AS SELECT 1;\n",
    b"CREATE TABLE public.t (v text);\nSET session_replication_role=replica;\n",
    b"CREATE TABLE public.t (v text);\nSET standard_conforming_strings=off;\n",
    b"CREATE TABLE public.t (v text);\nSET client_encoding='LATIN1';\n",
    b"CREATE TABLE public.t (v text);\nALTER TABLE public.t DISABLE TRIGGER ALL;\n",
    b"CREATE TABLE public.t (v text);\nSELECT pg_catalog.setval('public.s',42,true);\n",
    b"CREATE TABLE public.t (v text);\nGRANT ALL ON public.t TO PUBLIC;\n",
    b"CREATE TABLE public.t (v text);\nDO $$BEGIN PERFORM 1; END$$;\n",
    b"CREATE TABLE public.t (v text);\nCOMMIT;\n",
    b"CREATE TABLE public.t (v text);\n\x00\n",
])
def test_unknown_data_transaction_or_incomplete_paths_refuse_before_DDL(raw):
    with pytest.raises((ValueError, UnicodeDecodeError)):
        inputs.split_dump(raw)


def test_exact_review_seals_all_pieces_and_prevents_mutable_review_alias():
    value = review()
    raw = inputs.canonical(value)
    schema = inputs.ReviewedSchema(SQL, raw, inputs.sha(raw))
    assert schema.raw == SQL
    altered = schema.review
    altered["relations"].clear()
    assert len(schema.review["relations"]) == 21


@pytest.mark.parametrize("change", [
    dict(version="old"), dict(alembic_revision="head"), dict(whole_database_schema=False),
    dict(schema_sha256="c" * 64), dict(authority_sha256="0" * 64), dict(dump_frames_sha256="bad"),
    dict(statements=[]), dict(relations=[]), dict(exported_snapshot="latest"),
    dict(execution_policy="WHOLE_SCRIPT_WITH_UNPROVEN_TIMEOUT"), dict(review_id=""), dict(extra=True),
])
def test_unbound_partial_or_stale_review_is_not_private_restore_authority(change):
    value = review()
    value.update(change)
    raw = inputs.canonical(value)
    with pytest.raises(ValueError):
        inputs.ReviewedSchema(SQL, raw, inputs.sha(raw))


def test_one_changed_statement_even_same_byte_length_invalidates_review():
    raw = inputs.canonical(review())
    with pytest.raises(ValueError):
        inputs.ReviewedSchema(SQL.replace(b"integer", b"boolean"), raw, inputs.sha(raw))


def test_duplicate_review_keys_and_statement_reorder_are_refused():
    raw = inputs.canonical(review())
    duplicate = raw[:-1] + b',"review_id":"second"}'
    with pytest.raises(ValueError, match="duplicate"):
        inputs.ReviewedSchema(SQL, duplicate, inputs.sha(duplicate))
    value = deepcopy(review())
    value["statements"].reverse()
    raw = inputs.canonical(value)
    with pytest.raises(ValueError, match="every_raw_statement"):
        inputs.ReviewedSchema(SQL, raw, inputs.sha(raw))


def test_statement_and_byte_caps_do_not_truncate(monkeypatch):
    monkeypatch.setattr(inputs, "MAX_SQL", 1)
    with pytest.raises(ValueError):
        inputs.split_dump(SQL)
    monkeypatch.setattr(inputs, "MAX_SQL", 1024)
    monkeypatch.setattr(inputs, "MAX_RECORDS", 1)
    with pytest.raises(ValueError):
        inputs.split_dump(SQL)
