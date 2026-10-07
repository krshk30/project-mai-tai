"""Plugin mechanics only; these tests do not attest historical replay outcomes."""

import pytest
from pydantic import create_model

from combination_plugin import INSTALL2, install_settings_patch


def settings_model(**extra):
    fields = {key: (type(value), not value if isinstance(value, bool) else 1)
              for key, value in INSTALL2.items()}
    return create_model("CombinationSettings", **fields, **extra)


def test_install2_settings_are_validated_before_strategy_construction(monkeypatch):
    model = settings_model(retained_flag=(bool, False))
    install_settings_patch(monkeypatch, model)
    settings = model(retained_flag=True)
    assert settings.retained_flag is True
    assert {key: getattr(settings, key) for key in INSTALL2} == INSTALL2
    copied = settings.model_copy(update={"retained_flag": False})
    assert {key: getattr(copied, key) for key in INSTALL2} == INSTALL2


@pytest.mark.parametrize("missing", list(INSTALL2))
def test_install2_settings_refuse_a_partially_merged_candidate(monkeypatch, missing):
    fields = {key: (type(value), value) for key, value in INSTALL2.items() if key != missing}
    model = create_model("IncompleteSettings", **fields)
    with pytest.raises(AssertionError, match="incomplete Install2 tree"):
        install_settings_patch(monkeypatch, model)


def test_install2_settings_override_only_the_declared_live_delta(monkeypatch):
    model = settings_model(retained_flag=(bool, False))
    install_settings_patch(monkeypatch, model)
    settings = model(**{key: False if isinstance(value, bool) else 99
                        for key, value in INSTALL2.items()})
    assert settings.retained_flag is False
    assert {key: getattr(settings, key) for key in INSTALL2} == INSTALL2
