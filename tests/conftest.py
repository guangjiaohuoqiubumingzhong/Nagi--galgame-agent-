"""Never let tests read or write the developer's account, state or task history."""
import pytest


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NAGI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NAGI_APP_ROOT", str(tmp_path))
    for name in ("NAGI_HOME", "NAGI_LOCALE_SETTINGS"):
        monkeypatch.delenv(name, raising=False)
