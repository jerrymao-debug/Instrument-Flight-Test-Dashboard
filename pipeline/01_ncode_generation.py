from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from pathlib import Path

from pipeline_config import ASCII_TRANSLATE_EXE, CSV_DIR, FLIGHT_PHASE_FLOW, RAW_NCODE_DIR


OPEN_FLOW_FILE = False
TRANSLATE_TIMEOUT_SECONDS = 1800
APP_START_TIMEOUT_SECONDS = 60
PAGE_TIMEOUT_SECONDS = 300


def require_pywinauto():
    try:
        from pywinauto import Application
    except ImportError:
        print("Missing package: pywinauto")
        print("Install it with this command:")
        print("python -m pip install pywinauto pyperclip")
        raise SystemExit(1)

    return Application


def check_paths() -> None:
    if not ASCII_TRANSLATE_EXE.exists():
        raise FileNotFoundError(f"Could not find nCode ASCII Translate: {ASCII_TRANSLATE_EXE}")
    if not CSV_DIR.exists():
        raise FileNotFoundError(f"CSV folder does not exist: {CSV_DIR}")
    if OPEN_FLOW_FILE and not FLIGHT_PHASE_FLOW.exists():
        print(f"Warning: nCode flow file was not found: {FLIGHT_PHASE_FLOW}")
    RAW_NCODE_DIR.mkdir(parents=True, exist_ok=True)


def csv_files() -> list[Path]:
    return sorted(path for path in CSV_DIR.rglob("*.csv") if path.is_file())


def open_flow_file_once() -> None:
    if not OPEN_FLOW_FILE or not FLIGHT_PHASE_FLOW.exists():
        return
    print(f"Opening nCode flow: {FLIGHT_PHASE_FLOW}")
    os.startfile(str(FLIGHT_PHASE_FLOW))
    time.sleep(5)


def wait_for_page(
    window,
    page_title: str,
    timeout: float = PAGE_TIMEOUT_SECONDS,
    process: subprocess.Popen | None = None,
) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"ASCII Translate exited before page appeared: {page_title}")
        try:
            texts = [item.window_text() for item in window.descendants(control_type="Text")]
            if page_title in texts:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(0.25)
    detail = f" Last UI error: {last_error}" if last_error else ""
    raise TimeoutError(f"Timed out waiting for ASCII Translate page: {page_title}.{detail}")


def window_messages(window) -> list[str]:
    messages: list[str] = []
    for item in window.descendants():
        try:
            text = item.window_text()
        except Exception:
            continue
        if text:
            messages.append(text)
    return messages


def click_button(window, title: str) -> None:
    button = window.child_window(title=title, control_type="Button")
    try:
        button.invoke()
    except Exception:
        button.click_input()
    time.sleep(0.25)


def set_check_box(window, title: str, checked: bool) -> None:
    for check_box in window.descendants(control_type="CheckBox"):
        if check_box.window_text() == title:
            state = bool(check_box.get_toggle_state())
            if state != checked:
                check_box.click_input()
            return
    raise RuntimeError(f"Could not find checkbox: {title}")


def set_edit_text(edit, value: str) -> None:
    edit.set_focus()
    edit.set_edit_text(value)
    edit.type_keys("{TAB}")
    time.sleep(0.25)


def stop_process(process: subprocess.Popen, timeout: float = 10) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def fill_input_page(window, csv_path: Path) -> None:
    wait_for_page(window, "Input")
    edits = window.descendants(control_type="Edit")
    if not edits:
        raise RuntimeError("Could not find the input file field.")
    set_edit_text(edits[0], str(csv_path))
    set_check_box(window, "Use setup file", False)
    set_check_box(window, "Create log file", False)
    click_button(window, "Next")


def fill_layout_page(window) -> None:
    wait_for_page(window, "Layout")
    edits = window.descendants(control_type="Edit")
    if len(edits) < 4:
        raise RuntimeError("Could not find the layout fields.")

    set_edit_text(edits[0], "4")  # Number of header lines
    set_edit_text(edits[1], "")  # Number of channels, blank means auto-detect
    set_edit_text(edits[2], "2")  # Line number for channel titles
    set_edit_text(edits[3], "4")  # Line number for units

    set_check_box(window, "Tab separated", False)
    set_check_box(window, "Comma separated", True)
    set_check_box(window, "Space separated", False)
    set_check_box(window, "Semi-colon separated", False)
    set_check_box(window, "Fixed width", False)
    click_button(window, "Next")


def fill_time_series_details_page(window) -> None:
    wait_for_page(window, "Time Series Details")
    edits = window.descendants(control_type="Edit")
    if len(edits) < 5:
        raise RuntimeError("Could not find the time-series detail fields.")

    set_edit_text(edits[0], "5000")  # Sample rate
    set_check_box(window, "AutoDetect based on 1st column", False)
    set_edit_text(edits[1], "3")  # X-axis base
    set_edit_text(edits[2], "Time")  # X-axis title
    set_edit_text(edits[3], "Seconds")  # X-axis unit
    set_edit_text(edits[4], "")  # Name text
    set_check_box(window, "Write header lines to metadata", False)


def translate_one_csv(Application, csv_path: Path, overwrite: bool = False) -> Path:
    expected_intermediate = csv_path.with_suffix(".s3t")
    if expected_intermediate.exists():
        expected_intermediate.unlink()

    relative_output = csv_path.relative_to(CSV_DIR).with_suffix(".s3t")
    final_output = RAW_NCODE_DIR / relative_output
    final_output.parent.mkdir(parents=True, exist_ok=True)

    if final_output.exists() and not overwrite:
        print(f"SKIP existing output: {final_output}")
        return final_output
    if final_output.exists() and overwrite:
        final_output.unlink()

    print(f"Translating: {csv_path}", flush=True)
    process = subprocess.Popen(
        [str(ASCII_TRANSLATE_EXE)],
        cwd=str(ASCII_TRANSLATE_EXE.parent),
    )

    app = Application(backend="uia").connect(process=process.pid, timeout=APP_START_TIMEOUT_SECONDS)
    window = app.window(title="ASCII Translate")
    window.wait("visible enabled ready", timeout=APP_START_TIMEOUT_SECONDS)
    window.set_focus()

    try:
        fill_input_page(window, csv_path)
        fill_layout_page(window)
        fill_time_series_details_page(window)
        click_button(window, "Translate")

        wait_for_page(window, "Summary", timeout=TRANSLATE_TIMEOUT_SECONDS, process=process)
        messages = window_messages(window)
        if not any("translated successfully" in message.lower() for message in messages):
            raise RuntimeError("ASCII Translate finished without a success message.")

        click_button(window, "Finish")
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            stop_process(process)
    except Exception:
        stop_process(process)
        raise

    if process.returncode not in (0, None) and not expected_intermediate.exists():
        raise RuntimeError(f"ASCII Translate exited with code {process.returncode} for {csv_path}")
    if not expected_intermediate.exists():
        raise FileNotFoundError(f"nCode did not create expected output: {expected_intermediate}")

    shutil.move(str(expected_intermediate), str(final_output))
    print(f"Saved: {final_output}", flush=True)
    return final_output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Translate CSV files to nCode .s3t raw files.")
    parser.add_argument("--limit", type=int, default=None, help="Only translate the first N CSV files.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing .s3t outputs.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    Application = require_pywinauto()
    check_paths()

    files = csv_files()
    if args.limit is not None:
        files = files[: args.limit]
    if not files:
        print(f"No CSV files found in: {CSV_DIR}")
        return 0

    open_flow_file_once()
    completed = 0
    failed = 0
    for csv_path in files:
        try:
            translate_one_csv(Application, csv_path, overwrite=args.overwrite)
            completed += 1
        except Exception as exc:
            failed += 1
            print(f"FAILED: {csv_path}")
            print(f"        {exc}")

    print()
    print(f"Done. Completed: {completed}, failed: {failed}, output folder: {RAW_NCODE_DIR}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
        raise SystemExit(130)
