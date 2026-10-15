from project_mai_tai.settings import Settings


def test_sec_contact_default_empty_no_invented_contact(monkeypatch):
    monkeypatch.delenv("MAI_TAI_SEC_USER_AGENT", raising=False)
    settings = Settings(_env_file=None)
    assert settings.sec_user_agent == ""


def test_sec_contact_env_config_not_exposed_by_settings_repr(monkeypatch):
    value = "controlled-user-agent-only"
    monkeypatch.setenv("MAI_TAI_SEC_USER_AGENT", value)
    settings = Settings(_env_file=None)
    assert settings.sec_user_agent == value
    assert value not in repr(settings)
