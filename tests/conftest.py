from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Every test gets its own qbridge home, so nothing is written to the real
    ~/.qbridge (runs, diagnoses, experiments, audit log, cached address)."""
    monkeypatch.setenv("QBRIDGE_HOME", str(tmp_path_factory.mktemp("qbhome")))


@pytest.fixture
def fixture_text():
    def load(name: str) -> str:
        # Fixtures were saved from PowerShell, which may add a UTF-8 BOM.
        return (FIXTURES / name).read_text(encoding="utf-8-sig")
    return load
