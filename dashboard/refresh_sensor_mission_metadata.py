from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


SPREADSHEET_ID = "1VEjPb_A22omf4kReyR_GVEpnGpIQvjaXwcJDTjFxsZI"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "sensor_mission_metadata.json"
SENSOR_CONFIG_SHEET = "Sys Vibe Config"
GOOGLE_SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"


def fetch_json(url: str, access_token: str) -> dict:
    request = Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def read_values(sheet_name: str, cell_range: str, access_token: str) -> list[list[str]]:
    range_name = f"{sheet_name}!{cell_range}"
    url = (
        f"{GOOGLE_SHEETS_API}/{SPREADSHEET_ID}/values/"
        f"{quote(range_name, safe='')}?{urlencode({'valueRenderOption': 'FORMATTED_VALUE'})}"
    )
    payload = fetch_json(url, access_token)
    return payload.get("values", [])


def list_sheet_titles(access_token: str) -> list[str]:
    url = f"{GOOGLE_SHEETS_API}/{SPREADSHEET_ID}?fields=sheets.properties(title,sheetType,hidden)"
    payload = fetch_json(url, access_token)
    titles: list[str] = []
    for sheet in payload.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("sheetType") == "GRID":
            titles.append(properties.get("title", ""))
    return [title for title in titles if title]


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def normalize_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def normalize_sensor_type(value: str) -> str:
    text = normalize_space(value).lower()
    if "strain" in text:
        return "strain gauge"
    if "accelerometer" in text:
        return "accelerometer"
    return text


def date_to_iso(value: str) -> str:
    match = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", value or "")
    if not match:
        return ""
    month, day, year = (int(part) for part in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def exact_channels_for(location: str, sensor_type: str) -> list[str]:
    cleaned = normalize_space(location)
    normalized = cleaned.replace("-", "_").replace(" ", "_")
    channels = {normalized}
    no_channel_prefix = re.sub(r"^Ch\d+_", "", normalized, flags=re.IGNORECASE)
    channels.add(no_channel_prefix)

    lower = cleaned.lower()
    if sensor_type == "accelerometer" and "docking" in lower and "accel" in lower:
        if re.search(r"\bx\b", lower):
            channels.add("IMU_LIN_ACC_X_MPS2")
        if re.search(r"\by\b", lower):
            channels.add("IMU_LIN_ACC_Y_MPS2")
        if re.search(r"\bz\b", lower):
            channels.add("IMU_LIN_ACC_Z_MPS2")
    if "fwd cone" in lower:
        channels.add("Fwd_cone")
    if "fwd side 0" in lower:
        channels.add("Fwd_side_0")
    if "fwd side 45" in lower:
        channels.add("Fwd_side_45")
    if "fwd side 90" in lower:
        channels.add("Fwd_side_90")
    if "aft cone" in lower:
        channels.add("Aft_cone")
    if "aft side 0" in lower:
        channels.add("Aft_side_0")
    if "aft side 45" in lower:
        channels.add("Aft_side_45")
    if "aft side 90" in lower:
        channels.add("Aft_side_90")
    return sorted(channel for channel in channels if channel)


def campaign_from_config(config: str) -> str:
    match = re.search(r"zip\s*(\d+)", config, re.IGNORECASE)
    return match.group(1) if match else ""


def parse_sensors(rows: list[list[str]]) -> list[dict]:
    sensors: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    current_config = ""

    for row in rows:
        first = normalize_space(row[0]) if len(row) > 0 and row[0] else ""
        if first.lower().startswith("zip "):
            current_config = first
            continue
        if not current_config or len(row) < 4:
            continue

        location = normalize_space(row[2]) if len(row) > 2 and row[2] else ""
        sensor_type = normalize_sensor_type(row[3] if len(row) > 3 and row[3] else "")
        if not location or sensor_type not in {"accelerometer", "strain gauge"}:
            continue

        poc = normalize_space(row[15]) if len(row) > 15 and row[15] else ""
        key = (current_config, sensor_type, location)
        if key in seen:
            continue
        seen.add(key)

        sensor_id = f"{normalize_id(current_config)}_{normalize_id(location)}"
        exact_channels = exact_channels_for(location, sensor_type)
        sensors.append(
            {
                "id": sensor_id,
                "campaign": campaign_from_config(current_config),
                "config": current_config,
                "type": sensor_type,
                "location": location,
                "poc": poc,
                "aliases": sorted({location, location.replace("-", "_"), location.replace(" ", "_"), *exact_channels}),
                "exact_channels": exact_channels,
            }
        )
    return sensors


def parse_missions(sheet_name: str, rows: list[list[str]]) -> dict[str, dict]:
    missions: dict[str, dict] = {}
    for row in rows[1:]:
        aircraft = normalize_space(row[1]) if len(row) > 1 and row[1] else ""
        config = normalize_space(row[3]) if len(row) > 3 and row[3] else ""
        date = normalize_space(row[4]) if len(row) > 4 and row[4] else ""
        time_value = normalize_space(row[5]) if len(row) > 5 and row[5] else ""
        mission_type = normalize_space(row[7]) if len(row) > 7 and row[7] else ""
        mission_id = normalize_space(row[8]) if len(row) > 8 and row[8] else ""
        if not mission_id.startswith("P2M_"):
            continue
        missions[mission_id] = {
            "aircraft_id": aircraft,
            "config": config,
            "date": "" if date == "#N/A" else date,
            "date_iso": date_to_iso(date),
            "time": "" if time_value == "#N/A" else time_value,
            "mission_type": mission_type,
            "source_sheet": sheet_name,
        }
    return missions


def build_metadata(access_token: str) -> dict:
    sheet_titles = list_sheet_titles(access_token)
    test_log_titles = [title for title in sheet_titles if "test data log" in title.lower()]
    sensor_rows = read_values(SENSOR_CONFIG_SHEET, "A1:P1000", access_token)
    missions: dict[str, dict] = {}
    for title in test_log_titles:
        rows = read_values(title, "A1:I1000", access_token)
        missions.update(parse_missions(title, rows))
    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_spreadsheet_id": SPREADSHEET_ID,
        "sensors": parse_sensors(sensor_rows),
        "missions": missions,
    }


def write_metadata(metadata: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(output.parent)) as handle:
        temp_path = Path(handle.name)
        json.dump(metadata, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    temp_path.replace(output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh dashboard sensor and mission metadata from Google Sheets.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Path to write sensor_mission_metadata.json.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    access_token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN", "").strip()
    if not access_token:
        print("GOOGLE_OAUTH_ACCESS_TOKEN is required to read the private Google Sheet.")
        return 1
    metadata = build_metadata(access_token)
    output = Path(args.output).resolve()
    write_metadata(metadata, output)
    print(f"Wrote {output} with {len(metadata['sensors'])} sensors and {len(metadata['missions'])} missions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
