from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


SPREADSHEET_ID = "1VEjPb_A22omf4kReyR_GVEpnGpIQvjaXwcJDTjFxsZI"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "mission_aircraft_map.csv"
SHEET_SOURCES = [
    {"aircraft_id": "808", "sheet_name": "Zip808  Strain Test Data Log", "gid": "283584932"},
    {"aircraft_id": "1098", "sheet_name": "Zip1098  Strain Test Data Log", "gid": "843317299"},
    {"aircraft_id": "999", "sheet_name": "Zip999 Strain Test Data Log", "gid": "1699933123"},
]


def fetch_url(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> bytes:
    request = Request(url, headers=headers or {})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def read_sheet_with_google_api(sheet_name: str, access_token: str) -> list[list[str]]:
    range_name = f"{sheet_name}!B:I"
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/"
        f"{quote(range_name, safe='')}?{urlencode({'valueRenderOption': 'FORMATTED_VALUE'})}"
    )
    data = fetch_url(url, headers={"Authorization": f"Bearer {access_token}"})
    payload = json.loads(data.decode("utf-8"))
    return payload.get("values", [])


def read_sheet_with_csv_export(gid: str) -> list[list[str]]:
    url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/export?format=csv&gid={gid}"
    data = fetch_url(url)
    text = data.decode("utf-8-sig", errors="replace")
    if "<html" in text[:500].lower() or "ServiceLogin" in text[:2000]:
        raise RuntimeError("Google returned a login page instead of CSV.")
    return list(csv.reader(text.splitlines()))


def header_index(header: list[str], name: str) -> int:
    normalized = [cell.strip().lower() for cell in header]
    try:
        return normalized.index(name.lower())
    except ValueError as exc:
        raise RuntimeError(f"Could not find column {name!r} in exported sheet header: {header}") from exc


def rows_from_export(sheet: dict[str, str]) -> list[tuple[str, str, str, int]]:
    rows = read_sheet_with_csv_export(sheet["gid"])
    if not rows:
        return []
    header = rows[0]
    aircraft_index = header_index(header, "Aircraft ID")
    mission_index = header_index(header, "Mission ID")
    records: list[tuple[str, str, str, int]] = []
    for row_number, row in enumerate(rows[1:], start=2):
        aircraft = row[aircraft_index].strip() if aircraft_index < len(row) else ""
        mission = row[mission_index].strip() if mission_index < len(row) else ""
        if mission.startswith("P2M_") and aircraft:
            records.append((mission, aircraft, sheet["sheet_name"].replace("  ", " "), row_number))
    return records


def rows_from_api(sheet: dict[str, str], access_token: str) -> list[tuple[str, str, str, int]]:
    rows = read_sheet_with_google_api(sheet["sheet_name"], access_token)
    records: list[tuple[str, str, str, int]] = []
    for row_number, row in enumerate(rows[1:], start=2):
        aircraft = row[0].strip() if len(row) > 0 else ""
        mission = row[7].strip() if len(row) > 7 else ""
        if mission.startswith("P2M_") and aircraft:
            records.append((mission, aircraft, sheet["sheet_name"].replace("  ", " "), row_number))
    return records


def build_records(mode: str) -> list[tuple[str, str, str, int]]:
    access_token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN", "").strip()
    records: list[tuple[str, str, str, int]] = []
    errors: list[str] = []

    for sheet in SHEET_SOURCES:
        try:
            if mode == "api" or (mode == "auto" and access_token):
                if not access_token:
                    raise RuntimeError("GOOGLE_OAUTH_ACCESS_TOKEN is not set.")
                records.extend(rows_from_api(sheet, access_token))
            else:
                records.extend(rows_from_export(sheet))
        except (HTTPError, URLError, RuntimeError, json.JSONDecodeError) as exc:
            errors.append(f"{sheet['sheet_name']}: {exc}")

    if records:
        return records

    if mode == "auto" and not access_token:
        raise RuntimeError(
            "Could not refresh from Google Sheets. The sheet export needs login, and "
            "GOOGLE_OAUTH_ACCESS_TOKEN is not set.\n" + "\n".join(errors)
        )
    raise RuntimeError("Could not refresh from Google Sheets.\n" + "\n".join(errors))


def write_records(records: list[tuple[str, str, str, int]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    seen: dict[str, str] = {}
    for mission, aircraft, source_sheet, row_number in records:
        key = mission.upper()
        if key in seen and seen[key] != aircraft:
            raise RuntimeError(f"Mission {mission} maps to both {seen[key]} and {aircraft}.")
        seen[key] = aircraft

    with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", delete=False, dir=str(output.parent)) as handle:
        temp_path = Path(handle.name)
        writer = csv.writer(handle)
        writer.writerow(["mission_id", "aircraft_id", "source_sheet", "row"])
        for mission, aircraft, source_sheet, row_number in sorted(records, key=lambda item: (item[1], item[0])):
            writer.writerow([mission, aircraft, source_sheet, row_number])
    temp_path.replace(output)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh mission ID to aircraft ID map from Google Sheets.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Path to write mission_aircraft_map.csv.")
    parser.add_argument(
        "--mode",
        choices=["auto", "api", "export"],
        default="auto",
        help="auto uses GOOGLE_OAUTH_ACCESS_TOKEN when set, otherwise Google CSV export.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    output = Path(args.output).resolve()
    try:
        records = build_records(args.mode)
        write_records(records, output)
    except Exception as exc:
        print(f"Mission map refresh failed: {exc}", file=sys.stderr)
        return 1
    print(f"Refreshed mission aircraft map: {output} ({len(records)} mission rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
