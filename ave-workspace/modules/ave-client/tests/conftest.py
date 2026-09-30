import pytest


@pytest.fixture(autouse=True)
def isolate_database_root(tmp_path, monkeypatch):
    """Keep SQLite state isolated from the project db directory during tests."""
    monkeypatch.setenv("DB_ROOT", str(tmp_path / "db"))
