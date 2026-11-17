from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_text():
    def load(name: str) -> str:
        # Fixtures were saved from PowerShell, which may add a UTF-8 BOM.
        return (FIXTURES / name).read_text(encoding="utf-8-sig")
    return load
