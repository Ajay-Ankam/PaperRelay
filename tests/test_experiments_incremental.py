"""Incremental results.csv, run status, revision capture, stand-in config injection and
the waiting/working/retry partition flag (review findings on standin-steady-20261112-02)."""

from __future__ import annotations

import asyncio
import csv
import dataclasses
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from paperrelay.client.smoke import COLUMNS
from paperrelay.experiments import analyze
from paperrelay.experiments import load as load_mod
from paperrelay.experiments import run as run_mod
from paperrelay.experiments.client import build_row
from paperrelay.experiments.scenario import parse_scenario
from tests.test_experiments_load import done_payload, fake_api, read_rows, scenario_raw


def events_of(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (run_dir / "run_events.jsonl").read_text().splitlines()]


def status_of(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "results_status.json").read_text(encoding="utf-8"))


def split_api(run_dir: Path, fast: int) -> FastAPI:
    """The first `fast` requests to arrive finish at once; later ones hang for 30 s."""
    app = FastAPI()
    seen: list[str] = []

    @app.get("/config")
    async def config() -> dict[str, Any]:
        return {"source": "stand-in", "model": "fake", "run_id": "fake-run",
                "evidence_dir": str(run_dir)}

    @app.get("/capacity")
    async def capacity() -> dict[str, Any]:
        return {"queue_depth": 0}

    @app.post("/ask")
    async def ask(request: Request) -> Any:
        rid = (await request.json())["request_id"]
        seen.append(rid)
        hang = len(seen) > fast

        async def gen():
            if hang:
                await asyncio.sleep(30)
            yield json.dumps({"kind": "answer_token", "payload": {"text": "x"}}) + "\n"
            yield json.dumps({"kind": "done", "payload": done_payload(rid)}) + "\n"

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    return app


async def test_cancelled_run_keeps_finished_rows_and_is_marked_interrupted(tmp_path):
    """Mirrors the orchestrator's asyncio.wait_for(run_load(...)) firing mid-run."""
    sc = parse_scenario(scenario_raw(requests=4, window_s=0.4, client_timeout_s=10.0,
                                     max_runtime_s=15.0))
    planned = load_mod.plan(sc, sc.questions())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            load_mod.run_load(sc, api="http://t",
                              transport=httpx.ASGITransport(app=split_api(tmp_path, 2))),
            timeout=2.0)
    header, rows = read_rows(tmp_path / "results.csv")
    assert header == COLUMNS
    # Exactly the two finished requests, no placeholder rows for the two in flight.
    assert sorted(r["request_id"] for r in rows) == sorted(p.request_id for p in planned[:2])
    assert {r["status"] for r in rows} == {"ok"}
    obs = (tmp_path / "client_observations.jsonl").read_text().splitlines()
    assert len(obs) == 2
    st = status_of(tmp_path)
    assert st["run_status"] == "interrupted" and st["results_csv"] == "partial"
    assert st["rows_written"] == 2 and st["scheduled"] == 4
    assert {"revision_start", "revision_end", "revision_changed_during_run"} <= set(st)
    end = events_of(tmp_path)[-1]
    assert end["kind"] == "run_end" and end["run_status"] == "interrupted"
    assert not (tmp_path / "results.csv.tmp").exists()
    with pytest.raises(analyze.AnalysisError, match="partial"):
        analyze.load_run(tmp_path)


async def test_completed_run_status_and_revision_change_are_recorded(tmp_path, monkeypatch):
    revs = iter(["aaa", "bbb-dirty"])
    monkeypatch.setattr(load_mod, "git_revision", lambda: next(revs))
    app, _ = fake_api(tmp_path)
    sc = parse_scenario(scenario_raw())
    await load_mod.run_load(sc, api="http://t", transport=httpx.ASGITransport(app=app))
    end = events_of(tmp_path)[-1]
    assert end["run_status"] == "completed" and end["results_csv"] == "complete"
    assert (end["revision_start"], end["revision_end"]) == ("aaa", "bbb-dirty")
    assert end["revision_changed_during_run"] is True
    st = status_of(tmp_path)
    assert st["run_status"] == "completed" and st["revision_changed_during_run"] is True
    meta = json.loads((tmp_path / "scenario_meta.json").read_text())
    assert meta["revision_start"] == "aaa" == meta["runner_commit"]
    assert len(analyze.load_run(tmp_path).rows) == 4


async def test_runtime_cap_rows_are_appended_then_final_file_is_in_schedule_order(tmp_path):
    """Unsent and cap-hit requests get rows at the end; they must reach the incremental
    file too (verify_complete reads it back) and the final file is in schedule order."""
    app, _ = fake_api(tmp_path, mode="slow")
    sc = parse_scenario(scenario_raw(requests=4, window_s=4.0, client_timeout_s=10.0,
                                     max_runtime_s=15.0))
    # Bypass validation to force the runner cap at ~1 s: only the first arrival is sent.
    sc = dataclasses.replace(sc, max_runtime_s=1.2, server_record_wait_s=0.2)
    await load_mod.run_load(sc, api="http://t", transport=httpx.ASGITransport(app=app))
    _, rows = read_rows(tmp_path / "results.csv")
    planned = load_mod.plan(sc, sc.questions())
    assert [r["request_id"] for r in rows] == [p.request_id for p in planned]
    assert rows[0]["outcome_reason"] == "runner_runtime_cap"
    assert {r["outcome_reason"] for r in rows[1:]} == {"not_sent:runner_runtime_cap"}
    assert status_of(tmp_path)["run_status"] == "completed"


def test_verify_complete_rejects_missing_and_duplicate_rows(tmp_path):
    w = load_mod.IncrementalResults(tmp_path)
    base = {c: "" for c in COLUMNS}
    w.add_row({**base, "request_id": "a"})
    with pytest.raises(load_mod.LoadError):
        w.verify_complete(["a", "b"])
    w.add_row({**base, "request_id": "a"})
    with pytest.raises(load_mod.LoadError):
        w.verify_complete(["a", "b"])
    with pytest.raises(FileExistsError):  # the incremental writer never overwrites
        load_mod.IncrementalResults(tmp_path)


def test_failed_run_status_is_failed_not_interrupted(tmp_path, monkeypatch):
    app, _ = fake_api(tmp_path)
    sc = parse_scenario(scenario_raw())

    def boom(*a: Any, **k: Any) -> None:
        raise load_mod.LoadError("forced")

    monkeypatch.setattr(load_mod.IncrementalResults, "verify_complete", boom)
    with pytest.raises(load_mod.LoadError, match="forced"):
        asyncio.run(load_mod.run_load(sc, api="http://t",
                                      transport=httpx.ASGITransport(app=app)))
    assert status_of(tmp_path)["run_status"] == "failed"
    with open(tmp_path / "results.csv", newline="", encoding="utf-8") as fh:
        assert len(list(csv.DictReader(fh))) == 4  # rows survive; status says not final


# --------------------------------------------------------------------------- partition

def _obs(total_ms: float) -> dict[str, Any]:
    return {"request_id": "r", "retrieval_setting": "medium", "start_time": "t",
            "first_token_ms": None, "total_ms": total_ms, "client_reason": "client_abandoned"}


def test_partition_inconsistency_is_flagged_not_clamped():
    diag: dict[str, Any] = {}
    server = {"queue_wait_ms": 900.0, "rate_wait_ms": 0.0, "retry_wait_ms": 200.0}
    row = build_row(_obs(500.0), None, {"source": "stand-in"}, server, scenario="s",
                    question_id="q", extra={}, diagnostics=diag)
    assert row["working_ms"] == ""  # blank: not derivable, never a clamped 0
    assert row["waiting_ms"] == 900.0 and row["status"] == "timeout"
    assert diag["partition_inconsistent"] is True and diag["working_ms_raw"] == -600.0
    ok_diag: dict[str, Any] = {}
    row = build_row(_obs(1500.0), None, {"source": "stand-in"}, server, scenario="s",
                    question_id="q", extra={}, diagnostics=ok_diag)
    assert row["working_ms"] == 400.0 and ok_diag == {"partition_inconsistent": False}


# --------------------------------------------------------------------------- stand-in cfg

def test_standin_config_injects_calibration_and_replaces_stale_limits_note(tmp_path):
    base = json.loads(Path("standin/config-default.json").read_text(encoding="utf-8"))
    base["calibration"] = "standin/some-other-calibration.json"
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    sc = parse_scenario(scenario_raw(
        standin_config_base=str(base_path),
        calibration_file="standin/calibration-v1-preliminary.json",
        standin_config={"limits": {"rpm": 120, "tpm": 60000}}))
    cfg = run_mod.standin_config(sc)
    assert cfg["calibration"] == "standin/calibration-v1-preliminary.json"
    assert cfg["limits"] == {"rpm": 120, "tpm": 60000, "rpd": 1000, "tpd": 200000}
    assert not any("8000" in n and "smoke run" in n for n in cfg["notes"])
    [lim] = [n for n in cfg["notes"] if n.startswith("Limits:")]
    assert '"tpm": 60000' in lim and "removed" in lim
    # Non-limit notes from the base survive.
    assert any(n.startswith("Content is synthetic") for n in cfg["notes"])


def test_standin_config_conflicting_calibration_fails_fast():
    sc = parse_scenario(scenario_raw(
        calibration_file="standin/calibration-v1-preliminary.json",
        standin_config={"calibration": "standin/other.json"}))
    with pytest.raises(run_mod.RunError, match="calibration"):
        run_mod.standin_config(sc)


# --------------------------------------------------------------------------- git revision

@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_revision_marks_untracked_src_files_dirty(tmp_path, monkeypatch):
    import paperrelay.config as config_mod

    def git(*args: str) -> str:
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                               "-c", "commit.gpgsign=false", *args], cwd=tmp_path,
                              capture_output=True, text=True, check=True).stdout.strip()

    git("init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    git("add", "src/a.py")
    git("commit", "-q", "--no-verify", "-m", "init")
    sha = git("rev-parse", "HEAD")
    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path)
    assert config_mod.git_revision() == sha
    (tmp_path / "notes.txt").write_text("outside src\n")
    assert config_mod.git_revision() == sha  # untracked outside src/ does not count
    (tmp_path / "src" / "b.py").write_text("y = 2\n")
    assert config_mod.git_revision() == f"{sha}-dirty"
