"""The README's "How it uses QNX" table is how reviewers verify the QNX
connection, so every file it links must exist and every function or
identifier it names must appear in the file(s) of that row (a row without a
link continues the previous row's file)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _table_rows():
    sec = README.split("## How it uses QNX", 1)[1].split("\n## ", 1)[0]
    return [l for l in sec.splitlines() if l.startswith("| ") and not l.startswith("| Where")]


def _text_of(path: Path) -> str:
    if path.is_dir():
        return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                         for p in sorted(path.rglob("*")) if p.is_file())
    return path.read_text(encoding="utf-8", errors="replace")


def test_qnx_table_files_and_names_exist():
    rows = _table_rows()
    assert len(rows) >= 12
    current: list[Path] = []
    checked = 0
    for row in rows:
        where = row.split("|")[1]
        links = re.findall(r"\]\(([^)]+)\)", where)
        if links:
            current = [ROOT / l for l in links]
            for p in current:
                assert p.exists(), f"README links a missing path: {p}"
        text = "\n".join(_text_of(p) for p in current)
        for name in re.findall(r"`([A-Za-z_][\w]*)`", where):
            assert name in text, f"{name!r} is not in {[str(p.relative_to(ROOT)) for p in current]}"
            checked += 1
    assert checked >= 25


def test_readme_numbers_match_the_recorded_experiments():
    """The before/after tables quote the recorded experiments."""
    import json
    for name, before, after in (("experiment_live", "310, 304, 302", "**0, 0, 0**"),
                                ("experiment_live_overload", "301, 309, 305", "**0, 1, 0**")):
        e = json.loads((ROOT / "tests" / "fixtures" / name / "experiment.json").read_text(encoding="utf-8"))
        m = e["comparison"]["misses"]
        assert ", ".join(map(str, m["before"]["runs"])) == before
        assert "**" + ", ".join(map(str, m["after"]["runs"])) + "**" == after
        assert f"| {before} | {after} |" in README
