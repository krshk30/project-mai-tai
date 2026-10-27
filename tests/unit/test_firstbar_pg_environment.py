"""Controlled locale regression only; no PostgreSQL process starts here."""

from tools.firstbar_private_pg_store import postgres_environment


def test_pg_child_locale_is_explicit_without_changing_parent_environment(monkeypatch):
    monkeypatch.setenv("LC_ALL", "CONTROLLED_BAD_LOCALE")
    monkeypatch.setenv("LANG", "CONTROLLED_BAD_LANGUAGE")
    monkeypatch.setenv("CONTROLLED_OTHER", "retained")
    import os

    environment = postgres_environment()
    assert environment["LC_ALL"] == environment["LANG"] == "C"
    assert environment["CONTROLLED_OTHER"] == "retained"
    assert os.environ["LC_ALL"] == "CONTROLLED_BAD_LOCALE"
