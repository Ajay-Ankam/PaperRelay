"""Scenario files and seeded open-loop arrival schedules.

A scenario JSON (experiments/scenarios/<name>.json) fixes the workload: request count,
an explicit nonzero arrival window, the arrival process, the seed, client timeout,
server/stand-in configuration and timed control actions. Everything that changes the
workload is in the file, so its SHA-256 (`scenario_hash`) identifies the configuration.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from paperrelay.config import REPO_ROOT

Arrival = Literal["poisson", "uniform", "even"]
ARRIVALS: tuple[str, ...] = ("poisson", "uniform", "even")

# Hard bounds so a mistyped scenario cannot run away (stand-in quota is free, but CPU,
# disk and wall time are not).
HARD_MAX_REQUESTS = 200
HARD_MAX_RUNTIME_S = 900.0


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class ControlAction:
    at_s: float
    name: str
    api_limits: dict[str, Any] = field(default_factory=dict)  # POST API /admin/limits
    standin_config: dict[str, Any] = field(default_factory=dict)  # POST stand-in /admin/config


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    source: str
    seed: int
    requests: int
    window_s: float
    arrival: Arrival
    retrieval_setting: str
    questions_file: str
    client_timeout_s: float
    lag_threshold_ms: float
    max_requests: int
    max_runtime_s: float
    server_record_wait_s: float
    capacity_poll_s: float
    api_env: dict[str, str]
    standin_config_base: str | None
    standin_config: dict[str, Any]
    calibration_file: str | None
    control_actions: tuple[ControlAction, ...]
    raw: dict[str, Any]
    path: str = ""

    @property
    def scenario_hash(self) -> str:
        blob = json.dumps(self.raw, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def questions(self) -> list[str]:
        return load_questions(resolve(self.questions_file))


def resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


def load_questions(path: Path) -> list[str]:
    """One question per line; '#' lines are comments (the provenance header)."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            out.append(text)
    if not out:
        raise ScenarioError(f"{path} contains no questions")
    return out


def parse_scenario(raw: dict[str, Any], path: str = "") -> Scenario:
    def req(key: str) -> Any:
        if key not in raw:
            raise ScenarioError(f"scenario missing required key {key!r}")
        return raw[key]

    requests = int(req("requests"))
    window_s = float(req("window_s"))
    max_requests = int(raw.get("max_requests", 80))
    max_runtime_s = float(raw.get("max_runtime_s", 180.0))
    client_timeout_s = float(raw.get("client_timeout_s", 60.0))
    arrival = str(raw.get("arrival", "poisson"))
    if arrival not in ARRIVALS:
        raise ScenarioError(f"arrival must be one of {ARRIVALS}")
    if window_s <= 0:
        raise ScenarioError("window_s must be > 0 (a burst still has an explicit window)")
    if requests <= 0:
        raise ScenarioError("requests must be > 0 (no zero-request scenarios)")
    if max_requests > HARD_MAX_REQUESTS or max_runtime_s > HARD_MAX_RUNTIME_S:
        raise ScenarioError(
            f"max_requests <= {HARD_MAX_REQUESTS} and max_runtime_s <= {HARD_MAX_RUNTIME_S}")
    if requests > max_requests:
        raise ScenarioError(f"requests {requests} exceed max_requests {max_requests}")
    server_record_wait_s = float(raw.get("server_record_wait_s", 10.0))
    if window_s + client_timeout_s + server_record_wait_s > max_runtime_s:
        raise ScenarioError(
            "window_s + client_timeout_s + server_record_wait_s must fit within "
            "max_runtime_s so the last arrival can finish or be abandoned, and its server "
            "record collected, before the runtime cap")
    actions = []
    for item in raw.get("control_actions", []):
        at_s = float(item["at_s"])
        if not 0 < at_s < window_s:
            raise ScenarioError("control actions must happen inside the arrival window (mid-run)")
        actions.append(ControlAction(at_s=at_s, name=str(item.get("name", "action")),
                                     api_limits=dict(item.get("api_limits", {})),
                                     standin_config=dict(item.get("standin_config", {}))))
    api_env = {str(k): str(v) for k, v in raw.get("api_env", {}).items()}
    if api_env.get("PAPERRELAY_PROVIDER", "standin") == "groq":
        raise ScenarioError("scenario files never select the real provider")
    return Scenario(
        name=str(req("name")),
        description=str(raw.get("description", "")),
        source=str(raw.get("source", "stand-in")),
        seed=int(req("seed")),
        requests=requests,
        window_s=window_s,
        arrival=arrival,  # type: ignore[arg-type]
        retrieval_setting=str(raw.get("retrieval_setting", "medium")),
        questions_file=str(raw.get("questions_file", "experiments/workload_questions.txt")),
        client_timeout_s=client_timeout_s,
        lag_threshold_ms=float(raw.get("lag_threshold_ms", 250.0)),
        max_requests=max_requests,
        max_runtime_s=max_runtime_s,
        server_record_wait_s=server_record_wait_s,
        capacity_poll_s=float(raw.get("capacity_poll_s", 1.0)),
        api_env=api_env,
        standin_config_base=raw.get("standin_config_base"),
        standin_config=dict(raw.get("standin_config", {})),
        calibration_file=raw.get("calibration_file"),
        control_actions=tuple(sorted(actions, key=lambda a: a.at_s)),
        raw=raw,
        path=path,
    )


def load_scenario(path: Path) -> Scenario:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return parse_scenario(raw, str(path))


def arrival_offsets(n: int, window_s: float, arrival: str, seed: int) -> list[float]:
    """Seeded send offsets (seconds from run start) inside [0, window_s).

    - even: i * window/n (deterministic, no randomness).
    - uniform: n independent uniform draws in [0, window), sorted.
    - poisson: a Poisson process conditioned on n arrivals in the window. Conditioned on
      the count, arrival times are i.i.d. uniform order statistics, so this draws n
      exponential gaps, normalises their cumulative sum onto the window, and keeps the
      exact count (an unconditioned process could overshoot max_requests).
    """
    if n <= 0 or window_s <= 0:
        raise ScenarioError("need n > 0 and window_s > 0")
    rng = random.Random(seed)
    if arrival == "even":
        return [i * window_s / n for i in range(n)]
    if arrival == "uniform":
        return sorted(rng.uniform(0, window_s) for _ in range(n))
    if arrival == "poisson":
        gaps = [rng.expovariate(1.0) for _ in range(n + 1)]
        total = sum(gaps)
        out, acc = [], 0.0
        for g in gaps[:n]:
            acc += g
            out.append(window_s * acc / total)
        return out
    raise ScenarioError(f"unknown arrival {arrival!r}")


def request_id_for(seed: int, index: int, namespace: str = "load") -> str:
    """Deterministic request id: same seed + index -> same id across runs, so a
    request_id-seeded fault injector hits the same requests in paired runs (retry on/off)."""
    return hashlib.sha256(f"paperrelay:{namespace}:{seed}:{index}".encode()).hexdigest()[:32]


def question_for(index: int, questions: list[str], seed: int) -> tuple[str, str]:
    """(question_id, question): seeded shuffle of the pool, cycled."""
    order = list(range(len(questions)))
    random.Random(seed ^ 0x5EED).shuffle(order)
    q = order[index % len(order)]
    return f"w{q + 1:03d}", questions[q]
