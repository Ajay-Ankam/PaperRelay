"""Runtime settings from environment variables. Secrets are never stored on Settings."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[2]

ProviderName = Literal["offline", "groq"]
JsonMode = Literal["stream_prompt", "strict_schema"]


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    provider: ProviderName = "offline"
    groq_model: str = "openai/gpt-oss-20b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_json_mode: JsonMode = "stream_prompt"
    reasoning_effort: str = "low"
    deadline_s: float = 60.0
    call_timeout_s: float = 30.0
    retrieval_setting: str = "medium"
    index_dir: Path = REPO_ROOT / "data" / "indexes"
    manifest_path: Path = REPO_ROOT / "data" / "papers" / "manifest.json"
    evidence_dir: Path = REPO_ROOT / "evidence"
    run_id: str = ""
    real_call_ceiling: int | None = None

    @property
    def source(self) -> Literal["real", "offline-double"]:
        return "real" if self.provider == "groq" else "offline-double"

    def public(self) -> dict[str, object]:
        data = asdict(self)
        for key in ("index_dir", "manifest_path", "evidence_dir"):
            data[key] = str(data[key])
        return data


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ) if env is None else env
    provider = env.get("PAPERRELAY_PROVIDER", "offline").strip().lower()
    if provider not in ("offline", "groq"):
        raise ConfigError("PAPERRELAY_PROVIDER must be 'offline' or 'groq'")
    json_mode = env.get("PAPERRELAY_GROQ_JSON_MODE", "stream_prompt")
    if json_mode not in ("stream_prompt", "strict_schema"):
        raise ConfigError("PAPERRELAY_GROQ_JSON_MODE must be 'stream_prompt' or 'strict_schema'")

    ceiling_raw = env.get("PAPERRELAY_REAL_CALL_CEILING", "").strip()
    ceiling = int(ceiling_raw) if ceiling_raw else None
    run_id = env.get("PAPERRELAY_RUN_ID", "").strip()
    if provider == "groq":
        # Real calls are opt-in twice: provider choice plus an explicit hard ceiling and run id.
        if ceiling is None or ceiling <= 0:
            raise ConfigError("PAPERRELAY_PROVIDER=groq requires PAPERRELAY_REAL_CALL_CEILING > 0")
        if not run_id:
            raise ConfigError("PAPERRELAY_PROVIDER=groq requires PAPERRELAY_RUN_ID")
    if not run_id:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    if any(c in run_id for c in "/\\:") or run_id.startswith("."):
        raise ConfigError("PAPERRELAY_RUN_ID must be a simple name")

    defaults = Settings()
    return Settings(
        provider=provider,  # type: ignore[arg-type]
        groq_model=env.get("PAPERRELAY_GROQ_MODEL", defaults.groq_model),
        groq_json_mode=json_mode,  # type: ignore[arg-type]
        reasoning_effort=env.get("PAPERRELAY_REASONING_EFFORT", defaults.reasoning_effort),
        deadline_s=float(env.get("PAPERRELAY_DEADLINE_S", defaults.deadline_s)),
        call_timeout_s=float(env.get("PAPERRELAY_CALL_TIMEOUT_S", defaults.call_timeout_s)),
        retrieval_setting=env.get("PAPERRELAY_RETRIEVAL_SETTING", defaults.retrieval_setting),
        evidence_dir=Path(env.get("PAPERRELAY_EVIDENCE_DIR", str(defaults.evidence_dir))),
        run_id=run_id,
        real_call_ceiling=ceiling,
    )


def read_groq_api_key(env: dict[str, str] | None = None, dotenv: Path | None = None) -> str | None:
    """Return GROQ_API_KEY from the environment or .env without logging or echoing it."""
    env = dict(os.environ) if env is None else env
    if env.get("GROQ_API_KEY"):
        return env["GROQ_API_KEY"].strip()
    path = dotenv or REPO_ROOT / ".env"
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "GROQ_API_KEY":
            value = value.strip().strip('"').strip("'")
            return value or None
    return None


def git_revision() -> str:
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if not rev:
        return "unknown"
    return f"{rev}-dirty" if dirty else rev


def config_hash(settings: Settings, extra: dict[str, object]) -> str:
    payload = {k: v for k, v in settings.public().items() if k not in ("run_id", "evidence_dir")}
    payload.update(extra)
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]
