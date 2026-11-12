"""Stand-in configuration. Measured inputs come only from a calibration file; every other
number here is a synthetic, documented assumption (see docs/standin.md)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parents[3]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StageLatency(_Strict):
    first_content_median_ms: float = Field(ge=0)
    first_content_sigma: float = Field(default=0.6, ge=0)  # lognormal shape: assumption
    stream_median_ms: float = Field(ge=0)
    stream_sigma: float = Field(default=0.5, ge=0)
    completion_tokens: int = Field(ge=1)


class LatencyConfig(_Strict):
    # Empty -> taken from the calibration file's standin_parameters.
    stages: dict[str, StageLatency] = Field(default_factory=dict)
    tail_probability: float = Field(default=0.05, ge=0, le=1)
    tail_multiplier: float = Field(default=4.0, ge=1)
    slowdown_per_inflight: float = Field(default=0.25, ge=0)
    slowdown_free_inflight: int = Field(default=1, ge=0)
    time_scale: float = Field(default=1.0, gt=0)
    chunks: int = Field(default=6, ge=1)


class LimitsConfig(_Strict):
    # Header-advertised for gpt-oss-20b on the real smoke run: 1000 requests (RPD header),
    # 8000 tokens (TPM header). rpm/tpd follow documented free-tier values; verify docs.
    rpm: int = Field(default=30, ge=1)
    tpm: int = Field(default=8000, ge=1)
    rpd: int = Field(default=1000, ge=1)
    tpd: int = Field(default=200000, ge=1)


class FaultsConfig(_Strict):
    error_rate: float = Field(default=0.0, ge=0, le=1)
    error_status: int = Field(default=503, ge=500, le=599)
    malformed_rate: float = Field(default=0.0, ge=0, le=1)
    malformed_mode: Literal["truncate", "invalid_json"] = "truncate"


class ClockConfig(_Strict):
    day_offset_s: float = 0.0  # simulated UTC = real UTC + offset (day-boundary tests)


class StandinConfig(_Strict):
    model: str = "openai/gpt-oss-20b"
    seed: int = 0
    calibration: str | None = None
    usage_chars_per_token: float = Field(default=4.0, gt=0)  # synthetic usage reporting
    latency: LatencyConfig = Field(default_factory=LatencyConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    faults: FaultsConfig = Field(default_factory=FaultsConfig)
    clock: ClockConfig = Field(default_factory=ClockConfig)
    notes: list[str] = Field(default_factory=list)


def _deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def merge(config: StandinConfig, update: dict[str, Any]) -> StandinConfig:
    return StandinConfig.model_validate(_deep_merge(config.model_dump(), update))


def resolve_path(path: str, relative_to: Path | None) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    for base in (relative_to, REPO_ROOT, Path.cwd()):
        if base is not None and (base / p).exists():
            return base / p
    return REPO_ROOT / p


def with_calibration(config: StandinConfig, relative_to: Path | None = None
                     ) -> tuple[StandinConfig, dict[str, Any] | None]:
    """Fill missing stage latencies from the calibration file's standin_parameters."""
    if not config.calibration:
        if not config.latency.stages:
            raise ValueError("stand-in config needs latency.stages or a calibration file")
        return config, None
    cal = json.loads(resolve_path(config.calibration, relative_to).read_text(encoding="utf-8"))
    params = cal["standin_parameters"]
    stages = dict(config.latency.stages)
    for stage, p in params.items():
        if stage not in stages:
            stages[stage] = StageLatency(
                first_content_median_ms=p["first_content_median_ms"],
                stream_median_ms=p["stream_median_ms"],
                completion_tokens=p["completion_tokens"],
                first_content_sigma=p.get("first_content_sigma", 0.6),
                stream_sigma=p.get("stream_sigma", 0.5),
            )
    for needed in ("interpret", "draft", "verify"):
        if needed not in stages:
            raise ValueError(f"no latency parameters for stage {needed!r}")
    latency = config.latency.model_copy(update={"stages": stages})
    return config.model_copy(update={"latency": latency}), cal


def load_config(path: Path) -> tuple[StandinConfig, dict[str, Any] | None]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return with_calibration(StandinConfig.model_validate(raw), path.parent)
