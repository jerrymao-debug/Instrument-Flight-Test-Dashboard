from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path

from pipeline_config import (
    FLIGHT_PHASE_FLOW,
    FLOWPROC_EXE,
    PHASE_SPLIT_DIR,
    RAW_NCODE_DIR,
    TS_OUTPUT_GLYPH,
)


WORK_DIR = PHASE_SPLIT_DIR / "_flowproc_work"
PROCESSED_DIR = PHASE_SPLIT_DIR / "_processed"
GENERATED_EXTENSIONS = {".s3t", ".csv", ".xmh", ".xml"}


def check_paths() -> None:
    if not FLOWPROC_EXE.exists():
        raise FileNotFoundError(f"Could not find nCode Flowproc: {FLOWPROC_EXE}")
    if not FLIGHT_PHASE_FLOW.exists():
        raise FileNotFoundError(f"Could not find nCode flow: {FLIGHT_PHASE_FLOW}")
    if not RAW_NCODE_DIR.exists():
        raise FileNotFoundError(f"Could not find nCode raw folder: {RAW_NCODE_DIR}")

    PHASE_SPLIT_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)


def ncode_files() -> list[Path]:
    return sorted(path for path in RAW_NCODE_DIR.rglob("*.s3t") if path.is_file())


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_. -]+", "_", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "ncode_input"


def marker_path(input_file: Path) -> Path:
    return PROCESSED_DIR / f"{safe_name(input_file.stem)}.done"


def existing_outputs_for(input_file: Path) -> list[Path]:
    stem = safe_name(input_file.stem)
    return sorted(path for path in PHASE_SPLIT_DIR.glob(f"{stem}_*.s3t") if path.is_file())


def clean_outputs() -> int:
    removed = 0
    for path in PHASE_SPLIT_DIR.iterdir():
        if path.is_file() and path.suffix.lower() in GENERATED_EXTENSIONS:
            path.unlink()
            removed += 1
    if PROCESSED_DIR.exists():
        shutil.rmtree(PROCESSED_DIR)
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    return removed


def should_skip(input_file: Path, overwrite: bool) -> bool:
    if overwrite:
        return False
    marker = marker_path(input_file)
    existing_outputs = existing_outputs_for(input_file)
    if marker.exists() or existing_outputs:
        print(f"SKIP repeat/already processed: {input_file.name}")
        if existing_outputs:
            print(f"     Existing outputs found: {len(existing_outputs)}")
        return True
    return False


def quote_for_flowproc(value: Path | str) -> str:
    return str(value).replace('"', '""')


def working_flow_file() -> Path:
    copied_flow = WORK_DIR / FLIGHT_PHASE_FLOW.name
    shutil.copy2(FLIGHT_PHASE_FLOW, copied_flow)
    return copied_flow


def make_flow_script(input_file: Path) -> Path:
    stem = safe_name(input_file.stem)
    script_path = WORK_DIR / f"{stem}.script"
    output_name = PHASE_SPLIT_DIR / f"{stem}_TSfilt_#new.FeatureName#_#LoopData.CurrentLoop#"

    script_text = "\n".join(
        [
            f'SetProperty("{TS_OUTPUT_GLYPH}","NamingMethod","NewName")',
            f'SetProperty("{TS_OUTPUT_GLYPH}","NameText","{quote_for_flowproc(output_name)}")',
            f'SetProperty("{TS_OUTPUT_GLYPH}","FileFormat","S3TimeSeries")',
            f'SetProperty("{TS_OUTPUT_GLYPH}","Overwrite","Yes")',
            f'DoCommand(TSInput1, AddFiles, "{quote_for_flowproc(input_file)};ats")',
            "",
        ]
    )
    script_path.write_text(script_text, encoding="utf-8")
    return script_path


def run_flowproc(input_file: Path) -> None:
    script_path = make_flow_script(input_file)
    log_path = WORK_DIR / f"{safe_name(input_file.stem)}.batlog.lst"
    flow_file = working_flow_file()

    command = [
        str(FLOWPROC_EXE),
        f"/flow={flow_file}",
        f"/script={script_path}",
        "/verbose=yes",
        "/warnings=yes",
        f"/*={log_path}",
    ]

    print(f"RUN  {input_file.name}")
    result = subprocess.run(
        command,
        cwd=str(WORK_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    if result.stdout:
        print(result.stdout)
    if result.returncode != 0:
        print(f"Flowproc log: {log_path}")
        raise RuntimeError(f"flowproc failed with exit code {result.returncode}")

    outputs = existing_outputs_for(input_file)
    if not outputs:
        print(f"Flowproc log: {log_path}")
        raise RuntimeError("flowproc completed, but no matching .s3t outputs were found.")

    marker_path(input_file).write_text(
        f"input={input_file}\noutputs={len(outputs)}\n",
        encoding="utf-8",
    )
    print(f"SAVED {len(outputs)} output file(s) for {input_file.name}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run nCode flight phase split flow.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N unprocessed files.")
    parser.add_argument("--overwrite", action="store_true", help="Run even when matching outputs already exist.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    check_paths()
    if args.overwrite:
        removed = clean_outputs()
        if removed:
            print(f"Cleaned old phase split generated file(s): {removed}")

    files = ncode_files()
    if not files:
        print(f"No .s3t files found in: {RAW_NCODE_DIR}")
        return 0

    completed = 0
    skipped = 0
    failed = 0
    attempted = 0
    for input_file in files:
        if should_skip(input_file, args.overwrite):
            skipped += 1
            continue
        if args.limit is not None and attempted >= args.limit:
            print(f"Limit reached: {args.limit}")
            break
        attempted += 1
        try:
            run_flowproc(input_file)
            completed += 1
        except Exception as exc:
            failed += 1
            print(f"FAILED {input_file.name}")
            print(f"       {exc}")

    print()
    print(f"Done. Completed: {completed}, skipped: {skipped}, failed: {failed}")
    print(f"Output folder: {PHASE_SPLIT_DIR}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
        raise SystemExit(130)
