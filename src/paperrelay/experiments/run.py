"""Experiment orchestrator: stand-in + API subprocesses, then the open-loop load.

    python -m paperrelay.experiments.run --scenario experiments/scenarios/steady.json [--run-id X]

1. Refuses to start if evidence/<source>/<run_id>/ already exists (no overwrite).
2. Writes the stand-in config (optional base file deep-merged with the scenario's
   `standin_config`) into the run dir and starts `python -m paperrelay.standin`.
3. Starts the API (`python -m uvicorn paperrelay.api.main:app`, one worker) with
   PAPERRELAY_PROVIDER=standin, PAPERRELAY_ADMIN=1, a per-run PAPERRELAY_DAILY_STATE inside
   the run dir, PAPERRELAY_EVIDENCE_DIR and PAPERRELAY_RUN_ID, plus the scenario's api_env.
   GROQ_API_KEY is removed from the child environment.
4. Waits for both /health endpoints, runs the load, then stops both processes
   (CTRL_BREAK on Windows / SIGINT elsewhere, then terminate, then kill) and keeps their
   output as *_server_log.txt in the run dir (.log is git-ignored; these are evidence).

Bounds: the scenario's max_requests/max_runtime_s are validated before launch and the
load is additionally wrapped in a wall-clock limit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import httpx

from paperrelay.config import REPO_ROOT
from paperrelay.experiments.load import run_load, summarize
from paperrelay.experiments.scenario import Scenario, load_scenario, resolve

_SECRETISH = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH")


class RunError(RuntimeError):
    pass


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def standin_config(scenario: Scenario) -> dict[str, Any]:
    base: dict[str, Any] = {}
    if scenario.standin_config_base:
        path = resolve(scenario.standin_config_base)
        if not path.exists():
            raise RunError(f"stand-in base config {path} not found")
        base = json.loads(path.read_text(encoding="utf-8"))
    return deep_merge(base, scenario.standin_config)


def child_env(scenario: Scenario, *, run_id: str, run_dir: Path, evidence_root: Path,
              standin_port: int) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "GROQ_API_KEY"}
    env.update({
        "PAPERRELAY_PROVIDER": "standin",
        "PAPERRELAY_STANDIN_URL": f"http://127.0.0.1:{standin_port}/openai/v1",
        "PAPERRELAY_INDEX_DIR": str(REPO_ROOT / "data" / "indexes"),
    })
    env.update(scenario.api_env)
    # Forced last: the scenario cannot redirect evidence, daily state or the provider.
    env.update({
        "PAPERRELAY_PROVIDER": "standin",
        "PAPERRELAY_ADMIN": "1",
        "PAPERRELAY_DAILY_STATE": str(run_dir / "daily_budget.json"),
        "PAPERRELAY_EVIDENCE_DIR": str(evidence_root),
        "PAPERRELAY_RUN_ID": run_id,
        "PYTHONPATH": os.pathsep.join(
            [str(REPO_ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])),
        "PYTHONUNBUFFERED": "1",
    })
    return env


def public_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in sorted(env.items())
            if k.startswith("PAPERRELAY_") and not any(s in k.upper() for s in _SECRETISH)}


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class Proc:
    def __init__(self, name: str, cmd: list[str], env: dict[str, str], log_path: Path):
        self.name = name
        self.cmd = cmd
        self.log_path = log_path
        self._log: IO[bytes] = open(log_path, "wb")
        # Statement-form platform check: mypy narrows `if sys.platform == ...` blocks, but
        # not conditional expressions, and the constant only exists in Windows stubs.
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
        self.popen = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env, stdout=self._log,
                                      stderr=subprocess.STDOUT, creationflags=flags)
        self.started_utc = datetime.now(UTC).isoformat()
        self.stop_method = ""

    def alive(self) -> bool:
        return self.popen.poll() is None

    def stop(self, grace_s: float = 10.0) -> None:
        if self.alive():
            try:
                if sys.platform == "win32":
                    self.popen.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self.popen.send_signal(signal.SIGINT)
                self.popen.wait(timeout=grace_s)
                self.stop_method = "graceful"
            except (subprocess.TimeoutExpired, OSError):
                self.popen.terminate()
                try:
                    self.popen.wait(timeout=5)
                    self.stop_method = "terminate"
                except subprocess.TimeoutExpired:
                    self.popen.kill()
                    self.popen.wait(timeout=5)
                    self.stop_method = "kill"
        else:
            self.stop_method = "already_exited"
        self._log.close()

    def record(self) -> dict[str, Any]:
        return {"name": self.name, "cmd": self.cmd, "started_utc": self.started_utc,
                "returncode": self.popen.returncode, "stop_method": self.stop_method,
                "log": self.log_path.name}

    def tail(self, n: int = 2000) -> str:
        self._log.flush()
        data = self.log_path.read_bytes()[-n:]
        return data.decode("utf-8", errors="replace")


def wait_health(url: str, proc: Proc, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        if not proc.alive():
            raise RunError(f"{proc.name} exited early (code {proc.popen.returncode}):\n"
                           f"{proc.tail()}")
        try:
            resp = httpx.get(url, timeout=2.0)
            if resp.status_code == 200:
                return
            last = f"http_{resp.status_code}"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(0.3)
    raise RunError(f"{proc.name} not healthy at {url} within {timeout_s}s ({last})")


def orchestrate(scenario: Scenario, *, run_id: str, evidence_root: Path, api_port: int,
                standin_port: int, health_timeout_s: float, python: str,
                allow_uncalibrated: bool = False) -> Path:
    if scenario.source != "stand-in":
        raise RunError("the orchestrator only runs stand-in scenarios")
    calibrated = bool(scenario.calibration_file) and resolve(
        str(scenario.calibration_file)).exists()
    if not calibrated and not allow_uncalibrated:
        raise RunError("scenario calibration_file is unset or missing; set it to the "
                       "standin/calibration-*.json used, or pass --allow-uncalibrated for a "
                       "plumbing check (recorded as uncalibrated in orchestrator.json)")
    run_dir = evidence_root / scenario.source / run_id
    if run_dir.exists():
        raise RunError(f"{run_dir} already exists; choose a new --run-id (no overwrite)")
    for port in (api_port, standin_port):
        if not port_free(port):
            raise RunError(f"port {port} is in use")
    run_dir.mkdir(parents=True)
    cfg_path = run_dir / "standin_config.json"
    cfg_path.write_text(json.dumps(standin_config(scenario), indent=2), encoding="utf-8")
    env = child_env(scenario, run_id=run_id, run_dir=run_dir, evidence_root=evidence_root,
                    standin_port=standin_port)
    record: dict[str, Any] = {"run_id": run_id, "scenario": scenario.name,
                              "scenario_hash": scenario.scenario_hash,
                              "orchestrator_start_utc": datetime.now(UTC).isoformat(),
                              "calibrated": calibrated,
                              "api_port": api_port, "standin_port": standin_port,
                              "python": python, "child_env_public": public_env(env)}
    procs: list[Proc] = []
    status = "failed"
    try:
        standin = Proc("standin", [python, "-m", "paperrelay.standin", "--config",
                                   str(cfg_path), "--port", str(standin_port)],
                       env, run_dir / "standin_server_log.txt")
        procs.append(standin)
        wait_health(f"http://127.0.0.1:{standin_port}/health", standin, health_timeout_s)
        api = Proc("api", [python, "-m", "uvicorn", "paperrelay.api.main:app", "--host",
                           "127.0.0.1", "--port", str(api_port), "--workers", "1"],
                   env, run_dir / "api_server_log.txt")
        procs.append(api)
        wait_health(f"http://127.0.0.1:{api_port}/health", api, health_timeout_s)
        cfg = httpx.get(f"http://127.0.0.1:{api_port}/config", timeout=5.0).json()
        if Path(cfg.get("evidence_dir") or "").resolve() != run_dir.resolve():
            raise RunError(f"API evidence_dir {cfg.get('evidence_dir')} != {run_dir}")

        async def go() -> Any:
            return await asyncio.wait_for(
                run_load(scenario, api=f"http://127.0.0.1:{api_port}",
                         standin_url=f"http://127.0.0.1:{standin_port}",
                         run_meta_extra={"orchestrator": record}),
                timeout=scenario.max_runtime_s + 30.0)

        result = asyncio.run(go())
        print(f"rows={len(result.rows)} {summarize(result.rows)}")
        try:
            state = httpx.get(f"http://127.0.0.1:{standin_port}/admin/state", timeout=5.0)
            (run_dir / "standin_state_end.json").write_text(state.text, encoding="utf-8")
        except httpx.HTTPError as exc:
            record["standin_state_error"] = type(exc).__name__
        status = "completed"
    finally:
        for p in reversed(procs):
            p.stop()
        record["processes"] = [p.record() for p in procs]
        record["status"] = status
        record["orchestrator_end_utc"] = datetime.now(UTC).isoformat()
        (run_dir / "orchestrator.json").write_text(json.dumps(record, indent=2),
                                                   encoding="utf-8")
    return run_dir


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run one stand-in experiment scenario")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--evidence-root", default=str(REPO_ROOT / "evidence"))
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--standin-port", type=int, default=8100)
    parser.add_argument("--health-timeout-s", type=float, default=60.0)
    parser.add_argument("--allow-uncalibrated", action="store_true",
                        help="plumbing check without a calibration file (recorded)")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    scenario = load_scenario(Path(args.scenario))
    run_id = args.run_id or (
        f"{scenario.name}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}")
    if any(c in run_id for c in "/\\:") or run_id.startswith("."):
        parser.error("--run-id must be a simple name")
    try:
        run_dir = orchestrate(scenario, run_id=run_id, evidence_root=Path(args.evidence_root),
                              api_port=args.api_port, standin_port=args.standin_port,
                              health_timeout_s=args.health_timeout_s, python=sys.executable,
                              allow_uncalibrated=args.allow_uncalibrated)
    except RunError as exc:
        raise SystemExit(f"run failed: {exc}") from exc
    print(f"run dir: {run_dir}")


if __name__ == "__main__":
    main()
