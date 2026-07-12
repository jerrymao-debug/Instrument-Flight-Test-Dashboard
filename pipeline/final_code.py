from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


CODE_DIR = Path(__file__).resolve().parent

STEPS = [
    ("CSV to nCode raw", CODE_DIR / "01_ncode_generation.py"),
    ("Flight phase split", CODE_DIR / "02_flight_phase_split.py"),
    ("FDS ERS PSD generation", CODE_DIR / "04_FDS_SRS.py"),
]


def run_step(name: str, script: Path, extra_args: list[str]) -> int:
    if not script.exists():
        print(f"Missing script for step '{name}': {script}")
        return 1

    command = [sys.executable, str(script), *extra_args]
    print()
    print("=" * 80)
    print(f"STEP: {name}")
    print(f"RUN : {' '.join(command)}")
    print("=" * 80)

    result = subprocess.run(command, cwd=str(CODE_DIR))
    if result.returncode != 0:
        print()
        print(f"STOP: step failed: {name}")
        print(f"Exit code: {result.returncode}")
    return result.returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the complete new FDS nCode processing pipeline.")
    parser.add_argument("--skip-translate", action="store_true", help="Skip CSV to nCode raw translation.")
    parser.add_argument("--skip-split", action="store_true", help="Skip flight phase split.")
    parser.add_argument("--skip-fds", action="store_true", help="Skip FDS/ERS/PSD generation.")
    parser.add_argument("--limit", type=int, default=None, help="Pass a per-step processing limit for testing.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite/reprocess existing outputs.")
    return parser.parse_args()


def args_for_step(args: argparse.Namespace) -> list[str]:
    step_args: list[str] = []
    if args.limit is not None:
        step_args.extend(["--limit", str(args.limit)])
    if args.overwrite:
        step_args.append("--overwrite")
    return step_args


def main() -> int:
    args = parse_args()
    skip = {
        "CSV to nCode raw": args.skip_translate,
        "Flight phase split": args.skip_split,
        "FDS ERS PSD generation": args.skip_fds,
    }

    step_args = args_for_step(args)
    for name, script in STEPS:
        if skip[name]:
            print(f"SKIP step: {name}")
            continue
        returncode = run_step(name, script, step_args)
        if returncode != 0:
            return returncode

    print()
    print("Pipeline complete.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
        raise SystemExit(130)

