"""python -m paperrelay.standin --config standin/config-default.json --port 8100 [--seed N]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from paperrelay.standin.app import create_standin_app
from paperrelay.standin.config import load_config
from paperrelay.standin.sim import Simulator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m paperrelay.standin")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--seed", type=int, default=None, help="overrides config seed")
    parser.add_argument("--log", type=Path, default=None,
                        help="append one JSON line per call / admin change")
    args = parser.parse_args(argv)
    config, calibration = load_config(args.config)
    if args.seed is not None:
        config = config.model_copy(update={"seed": args.seed})
    sim = Simulator(config, calibration)
    if args.log is not None:
        args.log.parent.mkdir(parents=True, exist_ok=True)
    print(f"stand-in: model={config.model} seed={config.seed} content=synthetic-extractive "
          f"calibration={config.calibration} port={args.port}", file=sys.stderr)
    uvicorn.run(create_standin_app(sim, args.log), host=args.host, port=args.port,
                log_level="warning", workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
