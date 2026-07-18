from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path

from pipeline_config import FDS_SRS_FLOW, FLOWPROC_EXE, PHASE_SPLIT_DIR, PSD_OUTPUT_DIR, TS_INPUT_GLYPH


OUTPUTS = [
    ("HistogramOutput1 (Copy 1)", "_FDS", ".xmh", "nCodeXmlHistogram"),
    ("HistogramOutput1 (Copy 2)", "_ERS", ".xmh", "nCodeXmlHistogram"),
    ("HistogramOutput1 (Copy 3)", "_PSD", ".xmh", "nCodeXmlHistogram"),
    ("HistogramOutput1 (Copy 4)", "_PSD_Strain", ".xmh", "nCodeXmlHistogram"),
    ("HistogramOutput1 (Copy 5)", "_SRS", ".xmh", "nCodeXmlHistogram"),
]

WORK_DIR = PSD_OUTPUT_DIR / "_flowproc_work"
PROCESSED_DIR = PSD_OUTPUT_DIR / "_processed"
PHASE_BOUNDARY_RE = re.compile(
    r"_(?=(?:mixed|nosine|no_sine|port|stbd|fore|aft|channel_\d+|"
    r"(?:1st|1nd|2nd|4th|4nd|6th|6nd)_sine)(?:_|$))",
    re.IGNORECASE,
)
ORGANIZED_EXTENSIONS = {".csv", ".s3t", ".xmh", ".xml"}
PHASE_SIDECAR_EXTENSIONS = {".csv", ".xmh", ".xml"}


class LicenseUnavailableError(RuntimeError):
    pass


def check_paths() -> None:
    if not FLOWPROC_EXE.exists():
        raise FileNotFoundError(f"Could not find nCode Flowproc: {FLOWPROC_EXE}")
    if not FDS_SRS_FLOW.exists():
        raise FileNotFoundError(f"Could not find nCode flow: {FDS_SRS_FLOW}")
    try:
        with FDS_SRS_FLOW.open("rb") as flow:
            flow.read(1)
    except PermissionError as exc:
        raise PermissionError(
            "The FDS/SRS flow file is open or locked. Close the nCode GlyphWorks window for "
            f"{FDS_SRS_FLOW.name}, then run this script again."
        ) from exc
    if not PHASE_SPLIT_DIR.exists():
        raise FileNotFoundError(f"Could not find input folder: {PHASE_SPLIT_DIR}")

    PSD_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_. -]+", "_", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "ncode_input"


def flight_phase_from_name(value: str) -> str:
    stem = Path(value).stem
    if "_TSfilt_" in stem:
        stem = stem.split("_TSfilt_", 1)[1]

    marker = PHASE_BOUNDARY_RE.search(stem)
    if marker:
        stem = stem[: marker.start()]
    else:
        known_phase = re.search(
            r"(HoverTransit|Hover|Undocking|Docking|PreFlight|FixedWing)_\d+",
            stem,
            re.IGNORECASE,
        )
        if known_phase:
            stem = known_phase.group(0)
    return safe_name(stem.rstrip("_- ") or "Unsorted")


def output_dir_for(input_file: Path) -> Path:
    output_dir = PSD_OUTPUT_DIR / flight_phase_from_name(input_file.stem)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def channel_number(path: Path) -> int:
    match = re.search(r"_Channel_(\d+)$", path.stem)
    if match:
        return int(match.group(1))
    return 999999


def input_sort_key(path: Path) -> tuple[str, int, str]:
    parent = re.sub(r"_Channel_\d+$", "", path.stem)
    return parent.lower(), channel_number(path), path.stem.lower()


def input_files() -> list[Path]:
    return sorted(
        (
            path
            for path in PHASE_SPLIT_DIR.rglob("*.s3t")
            if path.is_file() and not any(part.startswith("_") for part in path.relative_to(PHASE_SPLIT_DIR).parts[:-1])
        ),
        key=input_sort_key,
    )


def marker_path(input_file: Path) -> Path:
    marker_dir = PROCESSED_DIR / flight_phase_from_name(input_file.stem)
    marker_dir.mkdir(parents=True, exist_ok=True)
    return marker_dir / f"{safe_name(input_file.stem)}.done"


def output_prefix(input_file: Path) -> str:
    return safe_name(input_file.stem)


def expected_outputs_for(input_file: Path) -> list[Path]:
    prefix = output_prefix(input_file)
    output_dir = output_dir_for(input_file)
    return [output_dir / f"{prefix}{suffix}{extension}" for _glyph, suffix, extension, _fmt in OUTPUTS]


def source_csv_for(input_file: Path) -> Path:
    return input_file.with_name(f"{input_file.stem}TAS.csv")


def output_csv_for(input_file: Path) -> Path:
    return output_dir_for(input_file) / f"{output_prefix(input_file)}TAS.csv"


def copy_csv_for(input_file: Path) -> Path | None:
    source = source_csv_for(input_file)
    if not source.exists():
        return None

    destination = output_csv_for(input_file)
    if not destination.exists() or source.stat().st_mtime > destination.stat().st_mtime:
        shutil.copy2(source, destination)
    return destination


def cleanup_phase_sidecars(input_file: Path) -> int:
    removed = 0
    for path in input_file.parent.glob(f"{input_file.stem}*"):
        if path == input_file:
            continue
        if path.is_file() and path.suffix.lower() in PHASE_SIDECAR_EXTENSIONS:
            path.unlink()
            removed += 1
    return removed


def existing_outputs_for(input_file: Path) -> list[Path]:
    outputs = [path for path in expected_outputs_for(input_file) if path.exists()]
    csv_output = output_csv_for(input_file)
    if csv_output.exists():
        outputs.append(csv_output)
    return outputs


def existing_plot_outputs_for(input_file: Path) -> list[Path]:
    return [path for path in expected_outputs_for(input_file) if path.exists()]


def should_skip(input_file: Path, overwrite: bool) -> bool:
    if overwrite:
        return False

    marker = marker_path(input_file)
    copy_csv_for(input_file)
    cleanup_phase_sidecars(input_file)
    existing_outputs = existing_plot_outputs_for(input_file)
    csv_output = output_csv_for(input_file)
    expected_count = len(OUTPUTS)
    if len(existing_outputs) == expected_count and csv_output.exists():
        print(f"SKIP repeat/already processed: {input_file.name}")
        print(f"     Existing .xmh outputs found: {len(existing_outputs)}, TAS CSV found")
        return True

    if existing_outputs:
        print(
            f"RECHECK: partial outputs found ({len(existing_outputs)}/{expected_count} xmh, "
            f"TAS={'yes' if csv_output.exists() else 'no'}), "
            f"rerunning: {input_file.name}"
        )
    elif marker.exists():
        print(f"RECHECK: marker exists but no output files were found, rerunning: {input_file.name}")
    return False


def clean_outputs() -> int:
    removed = 0
    for path in list(PSD_OUTPUT_DIR.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(PSD_OUTPUT_DIR)
        if any(part.startswith("_") for part in relative.parts[:-1]):
            continue
        if path.suffix.lower() in ORGANIZED_EXTENSIONS:
            path.unlink()
            removed += 1

    if PROCESSED_DIR.exists():
        shutil.rmtree(PROCESSED_DIR)
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    for directory in sorted(
        (path for path in PSD_OUTPUT_DIR.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if directory.name.startswith("_"):
            continue
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed


def print_repeat_summary(files: list[Path]) -> None:
    already_done = sum(
        1
        for input_file in files
        if len(existing_plot_outputs_for(input_file)) == len(OUTPUTS)
        and output_csv_for(input_file).exists()
    )
    remaining = len(files) - already_done
    output_count = len(list(PSD_OUTPUT_DIR.rglob("*.xmh"))) + len(list(PSD_OUTPUT_DIR.rglob("*.csv")))
    print(f"Input files found: {len(files)}")
    print(f"Existing .xmh/.csv output files found: {output_count}")
    print(f"Repeat check: {already_done} input file(s) already have all outputs and will be skipped.")
    print(f"Remaining input file(s) to run: {remaining}")


def quote_for_flowproc(value: Path | str) -> str:
    return str(value).replace('"', '""')


def make_flow_script(input_file: Path) -> Path:
    stem = output_prefix(input_file)
    phase = flight_phase_from_name(input_file.stem)
    work_dir = WORK_DIR / phase
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir = output_dir_for(input_file)
    script_path = work_dir / f"{stem}.script"
    lines: list[str] = []

    for glyph, suffix, _extension, file_format in OUTPUTS:
        output_name = output_dir / f"{stem}{suffix}"
        lines.append(f'SetProperty("{glyph}","NamingMethod","NewName")')
        lines.append(f'SetProperty("{glyph}","NameText","{quote_for_flowproc(output_name)}")')
        if file_format:
            lines.append(f'SetProperty("{glyph}","FileFormat","{file_format}")')
        lines.append(f'SetProperty("{glyph}","Overwrite","Yes")')

    lines.extend(
        [
            f'DoCommand({TS_INPUT_GLYPH}, AddFiles, "{quote_for_flowproc(input_file)};ats")',
            "",
        ]
    )
    script_path.write_text("\n".join(lines), encoding="utf-8")
    return script_path


def run_flowproc_once(input_file: Path) -> tuple[int, Path, str]:
    script_path = make_flow_script(input_file)
    log_path = script_path.with_suffix(".batlog.lst")

    command = [
        str(FLOWPROC_EXE),
        f"/flow={FDS_SRS_FLOW}",
        f"/script={script_path}",
        "/verbose=yes",
        "/warnings=yes",
        f"/*={log_path}",
    ]

    result = subprocess.run(
        command,
        cwd=str(WORK_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    log_text = ""
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    if result.stdout:
        log_text += "\n" + result.stdout
    return result.returncode, log_path, log_text


def is_license_error(log_text: str) -> bool:
    license_patterns = [
        "Failed to get license",
        "Licensing error",
        "Not enough licenses",
        "Licensed units limit",
        "GLYPH_ExtremeResponseSpectrum",
    ]
    return any(pattern.lower() in log_text.lower() for pattern in license_patterns)


def run_flowproc(input_file: Path) -> None:
    print(f"RUN  {input_file.name}")
    returncode, log_path, log_text = run_flowproc_once(input_file)

    if returncode != 0:
        print(f"Flowproc log: {log_path}")
        if is_license_error(log_text):
            raise LicenseUnavailableError(
                "nCode license is not available for FDS/SRS "
                "(usually GLYPH_ExtremeResponseSpectrum). Stop now and rerun later."
            )
        raise RuntimeError(f"flowproc failed with exit code {returncode}")

    csv_output = copy_csv_for(input_file)
    cleanup_phase_sidecars(input_file)
    outputs = existing_plot_outputs_for(input_file)
    expected_count = len(OUTPUTS)
    if len(outputs) != expected_count:
        print(f"Flowproc log: {log_path}")
        raise RuntimeError(
            f"flowproc completed, but only {len(outputs)} of {expected_count} expected .xmh output files were found."
        )

    saved_count = len(outputs) + (1 if csv_output else 0)
    marker_path(input_file).write_text(
        f"input={input_file}\nxmh_outputs={len(outputs)}\ncsv_output={csv_output or ''}\ninput_glyph={TS_INPUT_GLYPH}\n",
        encoding="utf-8",
    )
    print(f"SAVED {saved_count} output file(s) for {input_file.name}")


def organize_existing_outputs() -> int:
    moved = 0
    for path in list(PSD_OUTPUT_DIR.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(PSD_OUTPUT_DIR)
        if any(part.startswith("_") for part in relative.parts[:-1]):
            continue
        if path.suffix.lower() not in ORGANIZED_EXTENSIONS:
            continue

        phase_dir = PSD_OUTPUT_DIR / flight_phase_from_name(path.stem)
        destination = phase_dir / path.name
        if path.parent == phase_dir:
            continue

        phase_dir.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if path.stat().st_mtime > destination.stat().st_mtime:
                destination.unlink()
                shutil.move(str(path), str(destination))
                moved += 1
            else:
                path.unlink()
            continue

        shutil.move(str(path), str(destination))
        moved += 1

    for directory in sorted(
        (path for path in PSD_OUTPUT_DIR.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if directory.name.startswith("_"):
            continue
        try:
            directory.rmdir()
        except OSError:
            pass
    return moved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run nCode 4_FDS_SRS on phase split .s3t files.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N unprocessed files.")
    parser.add_argument("--overwrite", action="store_true", help="Run even when matching outputs already exist.")
    parser.add_argument("--organize-only", action="store_true", help="Move existing outputs into flight-phase folders, then exit.")
    parser.add_argument("--copy-tas-only", action="store_true", help="Copy TAS CSV files into phase output folders, then exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.copy_tas_only:
        if not PHASE_SPLIT_DIR.exists():
            raise FileNotFoundError(f"Could not find input folder: {PHASE_SPLIT_DIR}")
        PSD_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        files = input_files()
        copied = 0
        removed = 0
        for input_file in files:
            if copy_csv_for(input_file):
                copied += 1
            removed += cleanup_phase_sidecars(input_file)
        print(f"Copied TAS CSV file(s): {copied}")
        print(f"Cleaned phase sidecar file(s): {removed}")
        print(f"Output folder: {PSD_OUTPUT_DIR}")
        return 0

    check_paths()
    if args.overwrite:
        removed = clean_outputs()
        if removed:
            print(f"Cleaned old FDS/ERS/PSD/SRS output file(s): {removed}")
    moved = organize_existing_outputs()
    if moved:
        print(f"Organized existing output file(s) into flight-phase folders: {moved}")
    if args.organize_only:
        print(f"Organize-only complete. Output folder: {PSD_OUTPUT_DIR}")
        return 0

    files = input_files()
    if not files:
        print(f"No .s3t files found in: {PHASE_SPLIT_DIR}")
        return 0

    print_repeat_summary(files)
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
        except LicenseUnavailableError as exc:
            failed += 1
            print(f"STOPPED ON LICENSE ERROR at {input_file.name}")
            print(f"       {exc}")
            print()
            print(f"Done. Completed: {completed}, skipped: {skipped}, failed: {failed}")
            print(f"Output folder: {PSD_OUTPUT_DIR}")
            return 2
        except Exception as exc:
            failed += 1
            print(f"FAILED {input_file.name}")
            print(f"       {exc}")

    print()
    print(f"Done. Completed: {completed}, skipped: {skipped}, failed: {failed}")
    print(f"Output folder: {PSD_OUTPUT_DIR}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
