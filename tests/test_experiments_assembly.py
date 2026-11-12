"""Submission assembly: secret scan, exclusions, canonical results.csv, checklist."""

from __future__ import annotations

import csv
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

from paperrelay.client.smoke import COLUMNS

_SPEC = importlib.util.spec_from_file_location(
    "assemble_submission", Path(__file__).parents[1] / "scripts" / "assemble_submission.py")
assert _SPEC and _SPEC.loader
asm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(asm)

# Built at runtime so this test file itself never contains a key-shaped string.
FAKE_KEY = "gsk" + "_" + "Zx9" * 12


def results_csv(path: Path, source: str, rid: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerow({**{c: "" for c in COLUMNS}, "request_id": rid, "source": source,
                    "status": "ok"})


def make_repo(root: Path) -> list[Path]:
    files = {
        "src/pkg/a.py": "headers = {'Authorization': f'Bearer {key}'}\n",
        "docs/metrics.md": "Logs never contain API keys/Authorization.\n",
        ".env": f"GROQ_API_KEY={FAKE_KEY}\n",
        ".env.example": "GROQ_API_KEY=\n",
        "assignment_description.md": "private brief\n",
        "experiments/scenarios/steady.json": "{}\n",
        "eval/questions.csv": "question_id\n",
        "private/conversations/01-prompt.txt": "prompt text\n",
        "private/conversations/responses/01-response.txt": "response text\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    results_csv(root / "evidence" / "real" / "real-1" / "results.csv", "real", "a")
    results_csv(root / "evidence" / "stand-in" / "s-1" / "results.csv", "stand-in", "b")
    results_csv(root / "evidence" / "offline-double" / "o-1" / "results.csv",
                "offline-double", "c")
    return [Path(p) for p in files] + [Path("evidence/real/real-1/results.csv")]


def test_assembly_excludes_env_and_brief_and_builds_canonical_results(tmp_path):
    repo = tmp_path / "repo"
    files = make_repo(repo)
    out = tmp_path / "submission"
    result = asm.assemble(repo, out, files=files)
    names = zipfile.ZipFile(out / "source.zip").namelist()
    assert "src/pkg/a.py" in names and "docs/metrics.md" in names
    assert not any(n.startswith(".env") for n in names)
    assert "assignment_description.md" not in names
    with open(out / "results.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["source"] for r in rows] == ["real", "stand-in"]  # offline-double excluded
    manifest = json.loads((out / "results_manifest.json").read_text())
    assert manifest["rows"] == 2 and len(manifest["runs"]) == 2
    assert (out / "conversations" / "01-prompt.txt").exists()
    assert (out / "conversations" / "responses" / "01-response.txt").exists()
    assert not (out / "evidence" / "offline-double").exists()
    checklist = (out / "CHECKLIST.md").read_text()
    assert "report.pdf (written by Ajay): MISSING" in checklist
    assert "HUMAN ITEM" in checklist and "Secret scan: passed" in checklist
    assert result["secret_hits"] == []


def test_assembly_fails_on_planted_key(tmp_path):
    repo = tmp_path / "repo"
    files = make_repo(repo)
    (repo / "private" / "conversations" / "02-leak.txt").write_text(
        f"oops the key was {FAKE_KEY}\n", encoding="utf-8")
    with pytest.raises(asm.AssemblyError, match="secret patterns"):
        asm.assemble(repo, tmp_path / "submission", files=files)


def test_assembly_scans_source_zip_members(tmp_path):
    repo = tmp_path / "repo"
    files = make_repo(repo)
    (repo / "src" / "pkg" / "b.py").write_text(f"api_key = '{FAKE_KEY}'\n", encoding="utf-8")
    with pytest.raises(asm.AssemblyError, match="source.zip:src/pkg/b.py"):
        asm.assemble(repo, tmp_path / "submission", files=files + [Path("src/pkg/b.py")])


def test_scan_patterns_ignore_prose_but_catch_values():
    assert asm.scan_text("Logs never contain Authorization headers or Bearer tokens") == []
    assert asm.scan_text("f'Bearer {self._api_key}'") == []
    assert asm.scan_text(f"Authorization: Bearer {FAKE_KEY}")
    assert asm.scan_text("api_key" + "=" + "abcdefghijklmnopqrstuv")


def test_refuses_non_empty_output(tmp_path):
    repo = tmp_path / "repo"
    files = make_repo(repo)
    out = tmp_path / "submission"
    out.mkdir()
    (out / "x").write_text("x")
    with pytest.raises(asm.AssemblyError, match="not empty"):
        asm.assemble(repo, out, files=files)
