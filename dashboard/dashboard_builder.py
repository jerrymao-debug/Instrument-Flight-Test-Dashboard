from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import os
import re
import sys
import time
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse
import xml.etree.ElementTree as ET


SOURCE_S3_URI = "s3://vibration-data-daq/Instrumented fly test dashboard/"
DESTINATION_S3_URI = "s3://vibration-data-daq/insturment_fly_test_dashboard_code/"
PUBLIC_BASE_URL = "https://vibration-data-daq.s3.us-west-2.amazonaws.com/insturment_fly_test_dashboard_code"
LOCAL_BUILD_DIR = Path(r"C:\Users\jerry\Desktop\new FDS\dashboard_site")
LOCAL_CACHE_DIR = Path(r"C:\Users\jerry\Desktop\new FDS\dashboard_cache")
PREFERRED_AWS_PROFILE = "ncode-sso"

MAX_FREQ_POINTS = 950
MAX_TAS_POINTS = 2500
MAX_Y_COLUMNS = 12
MAX_XMH_CHANNELS = 96
MISSION_DOWNLOAD_EXPIRES_SECONDS = 604800
BUILDER_VERSION = "2026-07-12-static-dashboard-v5-mission-pages"

FLOAT_RE = re.compile(r"[-+]?(?:(?:\d+\.\d*)|(?:\.\d+)|(?:\d+))(?:[eE][-+]?\d+)?")
PHASE_BOUNDARY_RE = re.compile(
    r"_(?=(?:mixed|nosine|no_sine|port|stbd|fore|aft|channel_\d+|"
    r"(?:1st|1nd|2nd|4th|4nd|6th|6nd)_sine)(?:_|$))",
    re.IGNORECASE,
)
PHASE_ORDER = {
    "preflight": 0,
    "undocking": 1,
    "hover": 2,
    "hovertransit": 3,
    "fixedwing": 4,
    "docking": 5,
}
KIND_CONFIG = {
    "TAS": {
        "title": "TAS Time Display",
        "x_title": "Time",
        "y_title": "TAS (MPS)",
        "x_axis_type": "linear",
        "y_axis_type": "linear",
    },
    "ERS": {
        "title": "ERS Frequency Display",
        "x_title": "Frequency (Hz)",
        "y_title": "Extreme Response Acceleration (g)",
        "x_axis_type": "log",
        "y_axis_type": "log",
    },
    "FDS": {
        "title": "FDS Frequency Display",
        "x_title": "Frequency (Hz)",
        "y_title": "Damage",
        "x_axis_type": "log",
        "y_axis_type": "log",
    },
    "PSD": {
        "title": "PSD Frequency Display",
        "x_title": "Frequency (Hz)",
        "y_title": "PSD",
        "x_axis_type": "log",
        "y_axis_type": "log",
    },
    "STRAIN": {
        "title": "Strain Frequency Display",
        "x_title": "Frequency (Hz)",
        "y_title": "PSD (microstrain^2/Hz)",
        "x_axis_type": "log",
        "y_axis_type": "log",
    },
}
KIND_ORDER = ["TAS", "ERS", "FDS", "PSD", "STRAIN"]


@dataclass(frozen=True)
class S3Object:
    key: str
    size: int
    etag: str
    last_modified: datetime


def split_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Not a valid S3 URI: {uri}")
    prefix = parsed.path.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return parsed.netloc, prefix


def choose_aws_profile(requested_profile: str | None) -> str | None:
    if requested_profile:
        return requested_profile
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return None
    env_profile = os.environ.get("AWS_PROFILE")
    if env_profile:
        return env_profile
    try:
        import boto3
    except ImportError:
        return None
    profiles = set(boto3.Session().available_profiles)
    if PREFERRED_AWS_PROFILE in profiles:
        return PREFERRED_AWS_PROFILE
    if "default" in profiles:
        return "default"
    return None


def get_s3_client(profile: str | None):
    try:
        import boto3
    except ImportError:
        print("Missing package: boto3")
        print("Install it with: python -m pip install boto3")
        raise SystemExit(1)
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client("s3")


def local_cache_path(cache_dir: Path, bucket: str, key: str) -> Path:
    return cache_dir / bucket / Path(*PurePosixPath(key).parts)


def list_s3_objects(s3_client, bucket: str, prefix: str) -> list[S3Object]:
    objects: list[S3Object] = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        response = s3_client.list_objects_v2(**kwargs)
        for item in response.get("Contents", []):
            key = item["Key"]
            if key.endswith("/"):
                continue
            objects.append(
                S3Object(
                    key=key,
                    size=int(item.get("Size", 0)),
                    etag=str(item.get("ETag", "")).strip('"'),
                    last_modified=item["LastModified"],
                )
            )
        if not response.get("IsTruncated"):
            break
        token = response.get("NextContinuationToken")
    return sorted(objects, key=lambda obj: obj.key.lower())


def source_manifest_hash(objects: list[S3Object]) -> str:
    digest = hashlib.sha256()
    digest.update(BUILDER_VERSION.encode("utf-8"))
    for obj in objects:
        digest.update(obj.key.encode("utf-8"))
        digest.update(str(obj.size).encode("ascii"))
        digest.update(obj.etag.encode("ascii", errors="ignore"))
        digest.update(obj.last_modified.isoformat().encode("ascii", errors="ignore"))
    return digest.hexdigest()


def get_existing_manifest(s3_client, bucket: str, prefix: str) -> dict | None:
    key = f"{prefix}dashboard_manifest.json"
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
    except Exception:
        return None
    try:
        return json.loads(response["Body"].read().decode("utf-8"))
    except Exception:
        return None


def should_include_source_object(key: str) -> bool:
    name = PurePosixPath(key).name
    lower = name.lower()
    if not name or name.startswith("."):
        return False
    if "/_" in key:
        return False
    return lower.endswith(".xmh") or lower.endswith("tas.csv")


def campaign_from_relative(relative: PurePosixPath) -> str | None:
    if not relative.parts:
        return None
    campaign = relative.parts[0].strip("/")
    if not campaign or campaign.startswith("_"):
        return None
    return campaign


def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def safe_download_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "mission"


def public_url(base_url: str, relative_path: str) -> str:
    base = base_url.rstrip("/")
    encoded_path = "/".join(quote(part, safe="") for part in PurePosixPath(relative_path).parts)
    return f"{base}/{encoded_path}"


def natural_sort_key(value: str) -> list[int | str]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def frequency_group_rank(name: str) -> int:
    normalized = name.lower().replace("-", "_").replace(" ", "_")
    if "nosine" in normalized or "no_sine" in normalized:
        return 0
    if "1st_sine" in normalized or "1nd_sine" in normalized or "1_sine" in normalized:
        return 1
    if "2nd_sine" in normalized or "2_sine" in normalized:
        return 2
    if "4nd_sine" in normalized or "4th_sine" in normalized or "4_sine" in normalized:
        return 3
    if "6nd_sine" in normalized or "6th_sine" in normalized or "6_sine" in normalized:
        return 4
    return 99


def flight_phase_sort_key(value: str) -> tuple[int, int, list[int | str]]:
    normalized = value.replace(" ", "_")
    match = re.match(
        r"^(PreFlight|Undocking|HoverTransit|Hover|FixedWing|Docking)_?(\d+)?",
        normalized,
        re.IGNORECASE,
    )
    if not match:
        return (99, 0, natural_sort_key(value))
    phase_type = match.group(1).lower()
    phase_number = int(match.group(2) or 0)
    return (PHASE_ORDER.get(phase_type, 99), phase_number, natural_sort_key(value))


def safe_phase_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_. -]+", "_", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "Unsorted"


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
    return safe_phase_name(stem.rstrip("_- ") or "Unsorted")


def mission_id_from_name(value: str) -> str:
    stem = Path(value).stem
    match = re.search(r"(P2M_[A-Za-z0-9]+)", stem, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return "Unknown Mission"


def kind_from_name(value: str) -> str | None:
    upper = value.upper()
    if "_PSD_STRAIN" in upper:
        return "STRAIN"
    if upper.endswith("TAS.CSV") or "TAS.CSV" in upper:
        return "TAS"
    if "_ERS" in upper:
        return "ERS"
    if "_FDS" in upper:
        return "FDS"
    if "_PSD" in upper:
        return "PSD"
    return None


def normalize_match_text(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", value.upper())


def friendly_channel_name(title: str) -> str:
    upper = title.upper()
    axis = ""
    if "_X_" in upper or upper.endswith("_X"):
        axis = " X"
    elif "_Y_" in upper or upper.endswith("_Y"):
        axis = " Y"
    elif "_Z_" in upper or upper.endswith("_Z"):
        axis = " Z"
    prefix = []
    if "CAMERA_FORE" in upper:
        prefix.append("Camera Fore")
    elif "CAMERA_PORT" in upper:
        prefix.append("Camera Port")
    elif "PORT_" in upper and "IMU" not in upper:
        prefix.append("Port")
    elif "STBD_" in upper and "IMU" not in upper:
        prefix.append("Stbd")
    if "IMU" in upper:
        prefix.append("IMU")
    elif "ACCEL" in upper:
        prefix.append("ACCEL")
    if prefix:
        return " ".join(prefix) + axis
    return title.replace("_", " ")


def channel_sort_key(title: str) -> tuple[int, str]:
    normalized = normalize_match_text(title)
    if normalized.startswith("IMULINACC"):
        return (0, normalized)
    if "CAMERAFOREIMU" in normalized:
        return (1, normalized)
    if "CAMERAPORTIMU" in normalized:
        return (2, normalized)
    if "IMU" in normalized:
        return (3, normalized)
    if "ACCEL" in normalized:
        return (4, normalized)
    return (5, normalized)


def numbers_from_line(line: str) -> list[float]:
    values: list[float] = []
    for token in FLOAT_RE.findall(line):
        try:
            value = float(token)
        except ValueError:
            continue
        if math.isfinite(value):
            values.append(value)
    return values


def round_float(value: float) -> float:
    if value == 0:
        return 0.0
    magnitude = abs(value)
    if magnitude >= 1000:
        return round(value, 3)
    if magnitude >= 1:
        return round(value, 6)
    return float(f"{value:.6g}")


def downsample_xy(x_values: list[float], y_values: list[float], max_points: int) -> tuple[list[float], list[float], int]:
    count = min(len(x_values), len(y_values))
    if count <= max_points:
        return [round_float(v) for v in x_values[:count]], [round_float(v) for v in y_values[:count]], 0
    if max_points < 2:
        max_points = 2
    indexes = []
    for index in range(max_points):
        position = index * (count - 1) / (max_points - 1)
        indexes.append(int(round(position)))
    deduped = []
    seen = set()
    for index in indexes:
        if index not in seen:
            deduped.append(index)
            seen.add(index)
    return (
        [round_float(x_values[index]) for index in deduped],
        [round_float(y_values[index]) for index in deduped],
        count - len(deduped),
    )


def detect_encoding(path: Path) -> str:
    with path.open("rb") as handle:
        start = handle.read(4096)
    if start.startswith(b"\xff\xfe") or start.startswith(b"\xfe\xff"):
        return "utf-16"
    if start.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if start.count(b"\x00") > max(4, len(start) // 12):
        return "utf-16"
    return "utf-8-sig"


def mostly_sample_index(rows: list[list[float]], column_index: int) -> bool:
    if len(rows) < 4:
        return False
    sample = rows[: min(len(rows), 100)]
    first = sample[0][column_index]
    if not first.is_integer():
        return False
    expected = first
    for row in sample:
        if abs(row[column_index] - expected) > 0.001:
            return False
        expected += 1
    return True


def parse_ncode_csv(path: Path, file_id: str, kind: str) -> dict:
    encoding = detect_encoding(path)
    titles: list[str] = []
    units: list[str] = []
    data_rows: list[list[float]] = []
    section = ""
    warnings: list[str] = []

    try:
        handle = path.open("r", encoding=encoding, errors="replace", newline="")
    except OSError as exc:
        return {"traces": [], "warnings": [f"Could not open CSV: {exc}"]}

    with handle:
        reader = csv.reader(handle)
        for row in reader:
            if not row:
                continue
            first = row[0].strip()
            if first.startswith("#"):
                section = first.upper()
                continue
            if section == "#TITLES":
                titles = [cell.strip() for cell in row]
                continue
            if section == "#UNITS":
                units = [cell.strip() for cell in row]
                continue
            if section == "#DATA":
                values = []
                for cell in row:
                    try:
                        value = float(cell.strip())
                    except ValueError:
                        values = []
                        break
                    if not math.isfinite(value):
                        values = []
                        break
                    values.append(value)
                if len(values) >= 2:
                    data_rows.append(values)

    if not data_rows:
        return {"traces": [], "warnings": ["No numeric CSV data rows were found."]}

    width = min(Counter(len(row) for row in data_rows).most_common(1)[0][0], MAX_Y_COLUMNS + 2)
    rows = [row[:width] for row in data_rows if len(row) >= width]
    if not rows:
        return {"traces": [], "warnings": ["No complete numeric CSV rows were found."]}

    if kind == "TAS" and width >= 3 and mostly_sample_index(rows, 0):
        x_column = 1
        y_columns = list(range(2, width))
        x_label = "Time"
    else:
        x_column = 0
        y_columns = list(range(1, width))
        x_label = titles[x_column] if x_column < len(titles) and titles[x_column] else KIND_CONFIG[kind]["x_title"]

    x_values = [row[x_column] for row in rows]
    traces = []
    for column_index in y_columns:
        y_values = [row[column_index] for row in rows]
        title = titles[column_index] if column_index < len(titles) and titles[column_index] else f"Y{column_index}"
        unit = units[column_index] if column_index < len(units) and units[column_index] else ""
        y_label = f"{title} ({unit})" if unit else title
        trace_name = path.stem if len(y_columns) == 1 else f"{path.stem} - {title}"
        sampled_x, sampled_y, removed_count = downsample_xy(x_values, y_values, MAX_TAS_POINTS)
        if removed_count:
            warnings.append(f"{trace_name}: downsampled by {removed_count} points.")
        traces.append(
            {
                "name": trace_name,
                "file": file_id,
                "channel": title,
                "x": sampled_x,
                "y": sampled_y,
                "x_label": x_label,
                "y_label": y_label,
            }
        )
    return {"traces": traces, "warnings": sorted(set(warnings))}


def property_map(channel: ET.Element) -> dict[str, str]:
    properties: dict[str, str] = {}
    for prop in channel.findall("./Properties/Property"):
        name = prop.get("name")
        value = prop.get("value")
        if name is not None and value is not None:
            properties[name] = value
    return properties


def float_property(properties: dict[str, str], name: str) -> float | None:
    value = properties.get(name)
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def xmh_x_values(properties: dict[str, str], y_count: int) -> list[float]:
    x_min = float_property(properties, "XMin")
    x_max = float_property(properties, "XMax")
    bin_count_value = float_property(properties, "XBinCount")
    if x_min is None or x_max is None or bin_count_value is None or y_count <= 0:
        return [float(index + 1) for index in range(y_count)]
    bin_count = max(1, int(round(bin_count_value)))
    if bin_count == y_count:
        width = (x_max - x_min) / bin_count
        return [x_min + width * (index + 0.5) for index in range(y_count)]
    if y_count == 1:
        return [(x_min + x_max) / 2.0]
    width = (x_max - x_min) / (y_count - 1)
    return [x_min + width * index for index in range(y_count)]


def parse_xmh_histogram(path: Path, file_id: str, kind: str) -> dict:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return {"traces": [], "warnings": [f"Could not parse XMH XML: {exc}"]}
    channels = root.findall(".//HistogramChannel")
    if not channels:
        return {"traces": [], "warnings": ["No XMH HistogramChannel entries were found."]}

    warnings: list[str] = []
    traces = []
    if len(channels) > MAX_XMH_CHANNELS:
        warnings.append(f"Only the first {MAX_XMH_CHANNELS} XMH channels were plotted.")

    for channel in channels[:MAX_XMH_CHANNELS]:
        data_text = channel.findtext("Data") or ""
        y_values = numbers_from_line(data_text)
        if len(y_values) < 2:
            continue
        properties = property_map(channel)
        x_values = xmh_x_values(properties, len(y_values))
        positive_pairs = [
            (x_value, y_value)
            for x_value, y_value in zip(x_values, y_values)
            if math.isfinite(x_value) and math.isfinite(y_value) and x_value > 0 and y_value > 0
        ]
        if len(positive_pairs) < 2:
            continue
        x_values = [pair[0] for pair in positive_pairs]
        y_values = [pair[1] for pair in positive_pairs]
        title = channel.get("title") or f"Channel {channel.get('number', '')}".strip()
        number = channel.get("number")
        trace_title = f"{path.stem} - {title}" if title else path.stem
        if number:
            trace_title = f"{trace_title} [{number}]"

        x_title = properties.get("XTitle") or KIND_CONFIG[kind]["x_title"]
        x_units = properties.get("XUnits")
        y_title = properties.get("ZTitle") or KIND_CONFIG[kind]["y_title"]
        y_units = properties.get("ZUnits")
        if kind == "STRAIN":
            y_title = "PSD"
            y_units = "microstrain^2/Hz"
        x_label = f"{x_title} ({x_units})" if x_units else x_title
        y_label = f"{y_title} ({y_units})" if y_units else y_title
        sampled_x, sampled_y, removed_count = downsample_xy(x_values, y_values, MAX_FREQ_POINTS)
        if removed_count:
            warnings.append(f"{trace_title}: downsampled by {removed_count} points.")
        traces.append(
            {
                "name": trace_title,
                "file": file_id,
                "channel": title,
                "channel_key": normalize_match_text(title),
                "x": sampled_x,
                "y": sampled_y,
                "x_label": x_label,
                "y_label": y_label,
            }
        )
    if not traces:
        warnings.append("No positive XMH channel data was found to plot.")
    return {"traces": traces, "warnings": sorted(set(warnings))}


def parse_data_file(path: Path, file_id: str, kind: str) -> dict:
    if path.suffix.lower() == ".xmh":
        return parse_xmh_histogram(path, file_id, kind)
    if path.suffix.lower() == ".csv":
        return parse_ncode_csv(path, file_id, kind)
    return {"traces": [], "warnings": [f"Unsupported file type: {path.suffix}"]}


def record_sort_key(record: dict) -> tuple:
    if record["kind"] == "TAS":
        return (flight_phase_sort_key(record["phase"]), record["mission"], record["name"].lower())
    return (
        flight_phase_sort_key(record["phase"]),
        frequency_group_rank(record["name"]),
        record["mission"],
        record["name"].lower(),
    )


def tas_representative_rank(record: dict) -> tuple[int, str]:
    name = record["name"]
    rank = frequency_group_rank(name)
    normalized = name.lower()
    if "tas" in normalized and ("nosine" in normalized or "no_sine" in normalized):
        rank = -1
    return (rank, normalized)


def representative_records_by_phase(records: list[dict]) -> list[dict]:
    by_scope: dict[tuple[str, str], list[dict]] = {}
    for record in records:
        by_scope.setdefault((record["mission"], record["phase"]), []).append(record)
    representatives = []
    for (_, _), phase_records in sorted(
        by_scope.items(),
        key=lambda item: (natural_sort_key(item[0][0]), flight_phase_sort_key(item[0][1])),
    ):
        representatives.append(sorted(phase_records, key=tas_representative_rank)[0])
    return representatives


def selector_records(kind: str, records: list[dict]) -> list[dict]:
    if kind == "TAS":
        return representative_records_by_phase(records)
    return records


def selectors_for_group(kind: str, records: list[dict]) -> list[dict]:
    selectors = [
        {
            "id": "all",
            "type": "all",
            "number": "All",
            "name": f"{kind} (All)",
            "detail": "Show matching plots",
            "phase": "all",
            "mission": "all",
        }
    ]
    for file_index, record in enumerate(selector_records(kind, records), 1):
        selectors.append(
            {
                "id": record["id"],
                "type": "file",
                "number": f"File {file_index}",
                "name": record["name"],
                "detail": f"{record['mission']} | {record['phase']} | File #{file_index} | {record['size_label']}",
                "file": record["id"],
                "file_name": record["name"],
                "source_name": record["name"],
                "phase": record["phase"],
                "mission": record["mission"],
                "download_url": record.get("download_url", ""),
                "download_s3_uri": record.get("download_s3_uri", ""),
            }
        )
    return selectors


def empty_group_counts() -> dict[str, int]:
    return {kind: 0 for kind in KIND_ORDER}


def build_campaign_payload(
    campaign: str,
    source_prefix: str,
    objects: list[S3Object],
    cache_dir: Path,
    bucket: str,
    mission_downloads: dict[str, dict[str, dict]],
    source_downloads: dict[str, dict],
) -> dict:
    groups = {
        kind: {
            "key": kind,
            "title": KIND_CONFIG[kind]["title"],
            "x_title": KIND_CONFIG[kind]["x_title"],
            "y_title": KIND_CONFIG[kind]["y_title"],
            "x_axis_type": KIND_CONFIG[kind]["x_axis_type"],
            "y_axis_type": KIND_CONFIG[kind]["y_axis_type"],
            "files": [],
            "selectors": [],
        }
        for kind in KIND_ORDER
    }
    series: dict[str, dict] = {}
    phase_stats: dict[str, dict] = {}
    mission_stats: dict[str, dict] = {}
    channels: dict[str, dict] = {}

    for obj in objects:
        relative = PurePosixPath(obj.key[len(source_prefix) :])
        if campaign_from_relative(relative) != campaign:
            continue
        if not should_include_source_object(obj.key):
            continue
        kind = kind_from_name(relative.name)
        if kind is None:
            continue
        path = local_cache_path(cache_dir, bucket, obj.key)
        campaign_relative = PurePosixPath(*relative.parts[1:])
        if len(campaign_relative.parts) > 1 and not campaign_relative.parts[0].startswith("_"):
            phase = safe_phase_name(campaign_relative.parts[0])
        else:
            phase = flight_phase_from_name(relative.name)
        mission = mission_id_from_name(relative.name)
        file_id = f"{campaign}/{campaign_relative.as_posix()}"
        record = {
            "id": file_id,
            "kind": kind,
            "name": relative.name,
            "relative_path": campaign_relative.as_posix(),
            "phase": phase,
            "mission": mission,
            "size": obj.size,
            "size_label": human_size(obj.size),
            "modified": obj.last_modified.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "suffix": Path(relative.name).suffix.lower(),
        }
        download = source_downloads.get(file_id)
        if download:
            record["download_url"] = download["url"]
            record["download_s3_uri"] = download["s3_uri"]
        parsed = parse_data_file(path, file_id, kind)
        if not parsed["traces"]:
            record["warnings"] = parsed["warnings"]
        series[file_id] = parsed
        groups[kind]["files"].append(record)

        phase_stat = phase_stats.setdefault(
            phase,
            {"id": phase, "name": phase, "file_count": 0, "groups": empty_group_counts()},
        )
        mission_stat = mission_stats.setdefault(
            mission,
            {"id": mission, "name": mission, "file_count": 0, "groups": empty_group_counts()},
        )
        phase_stat["file_count"] += 1
        phase_stat["groups"][kind] += 1
        mission_stat["file_count"] += 1
        mission_stat["groups"][kind] += 1

        if kind not in {"TAS", "STRAIN"}:
            for trace in parsed["traces"]:
                channel = trace.get("channel") or ""
                key = normalize_match_text(channel)
                if not key:
                    continue
                option = channels.setdefault(
                    key,
                    {
                        "id": f"exact:{channel}",
                        "name": friendly_channel_name(channel),
                        "detail": channel,
                        "token": f"exact:{channel}",
                        "count": 0,
                    },
                )
                option["count"] += 1

    for kind in KIND_ORDER:
        groups[kind]["files"] = sorted(groups[kind]["files"], key=record_sort_key)
        groups[kind]["selectors"] = selectors_for_group(kind, groups[kind]["files"])

    channel_options = [
        {"id": "all", "name": "All Sensors", "detail": "No accelerometer sensor filter", "token": "all", "count": 0}
    ]
    channel_options.extend(sorted(channels.values(), key=lambda option: channel_sort_key(option["detail"])))

    missions = sorted(mission_stats.values(), key=lambda item: natural_sort_key(item["name"]))
    for mission in missions:
        download = mission_downloads.get(campaign, {}).get(mission["id"])
        if download:
            mission["download_url"] = download["url"]
            mission["download_size_label"] = download["size_label"]
            mission["download_file_count"] = download["file_count"]
            mission["download_s3_uri"] = download["s3_uri"]

    return {
        "id": campaign,
        "name": campaign,
        "total_files": sum(len(groups[kind]["files"]) for kind in KIND_ORDER),
        "missions": missions,
        "phases": sorted(phase_stats.values(), key=lambda item: flight_phase_sort_key(item["name"])),
        "groups": groups,
        "series": series,
        "channels": channel_options,
    }


def download_changed_objects(s3_client, bucket: str, objects: list[S3Object], cache_dir: Path) -> None:
    for index, obj in enumerate(objects, start=1):
        if not should_include_source_object(obj.key):
            continue
        target = local_cache_path(cache_dir, bucket, obj.key)
        marker = target.with_suffix(target.suffix + ".etag")
        if target.exists() and marker.exists() and marker.read_text(encoding="utf-8") == obj.etag:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"download [{index}/{len(objects)}]: {obj.key}")
        s3_client.download_file(bucket, obj.key, str(target))
        marker.write_text(obj.etag, encoding="utf-8")


def build_mission_downloads(
    s3_client,
    source_bucket: str,
    source_prefix: str,
    source_objects: list[S3Object],
    cache_dir: Path,
    destination_bucket: str,
    destination_prefix: str,
    public_base_url: str | None = None,
) -> dict[str, dict[str, dict]]:
    grouped: dict[tuple[str, str], list[S3Object]] = {}
    for obj in source_objects:
        if not should_include_source_object(obj.key):
            continue
        relative = PurePosixPath(obj.key[len(source_prefix) :])
        campaign = campaign_from_relative(relative)
        if not campaign:
            continue
        mission = mission_id_from_name(relative.name)
        if mission == "Unknown Mission":
            continue
        grouped.setdefault((campaign, mission), []).append(obj)

    downloads: dict[str, dict[str, dict]] = {}
    zip_root = cache_dir / "_mission_downloads"
    for (campaign, mission), mission_objects in sorted(grouped.items(), key=lambda item: (natural_sort_key(item[0][0]), natural_sort_key(item[0][1]))):
        source_signature = hashlib.sha256(
            "\n".join(f"{obj.key}\t{obj.etag}\t{obj.size}" for obj in sorted(mission_objects, key=lambda item: item.key)).encode("utf-8")
        ).hexdigest()
        zip_dir = zip_root / safe_download_name(campaign)
        zip_dir.mkdir(parents=True, exist_ok=True)
        zip_path = zip_dir / f"{safe_download_name(mission)}.zip"
        marker = zip_path.with_suffix(".zip.sha256")
        if not zip_path.exists() or not marker.exists() or marker.read_text(encoding="utf-8") != source_signature:
            print(f"Create mission download: {campaign}/{mission} ({len(mission_objects)} files)")
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for obj in sorted(mission_objects, key=lambda item: item.key):
                    local_path = local_cache_path(cache_dir, source_bucket, obj.key)
                    if not local_path.exists():
                        continue
                    relative = PurePosixPath(obj.key[len(source_prefix) :])
                    arcname = PurePosixPath(*relative.parts[1:]).as_posix() if len(relative.parts) > 1 else relative.name
                    archive.write(local_path, arcname)
            marker.write_text(source_signature, encoding="utf-8")

        download_relative = f"downloads/{safe_download_name(campaign)}/{safe_download_name(mission)}.zip"
        key = f"{destination_prefix}{download_relative}"
        print(f"Upload mission download: s3://{destination_bucket}/{key} ({human_size(zip_path.stat().st_size)})")
        s3_client.upload_file(
            str(zip_path),
            destination_bucket,
            key,
            ExtraArgs={
                "ContentType": "application/zip",
                "ContentDisposition": f'attachment; filename="{safe_download_name(campaign)}_{safe_download_name(mission)}.zip"',
                "CacheControl": "no-cache",
            },
        )
        if public_base_url:
            url = public_url(public_base_url, download_relative)
        else:
            url = s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": destination_bucket, "Key": key},
                ExpiresIn=MISSION_DOWNLOAD_EXPIRES_SECONDS,
            )
        downloads.setdefault(campaign, {})[mission] = {
            "url": url,
            "s3_uri": f"s3://{destination_bucket}/{key}",
            "size": zip_path.stat().st_size,
            "size_label": human_size(zip_path.stat().st_size),
            "file_count": len(mission_objects),
        }
    return downloads


def build_source_downloads(
    s3_client,
    source_bucket: str,
    source_prefix: str,
    source_objects: list[S3Object],
    destination_bucket: str | None = None,
    destination_prefix: str | None = None,
    public_base_url: str | None = None,
) -> dict[str, dict]:
    downloads: dict[str, dict] = {}
    for obj in source_objects:
        if not should_include_source_object(obj.key):
            continue
        relative = PurePosixPath(obj.key[len(source_prefix) :])
        campaign = campaign_from_relative(relative)
        if not campaign or len(relative.parts) <= 1:
            continue
        campaign_relative = PurePosixPath(*relative.parts[1:])
        file_id = f"{campaign}/{campaign_relative.as_posix()}"
        filename = PurePosixPath(obj.key).name.replace('"', "_").replace("\\", "_")
        s3_uri = f"s3://{source_bucket}/{obj.key}"
        if public_base_url and destination_bucket and destination_prefix is not None:
            mirror_relative = f"source-files/{file_id}"
            mirror_key = f"{destination_prefix}{mirror_relative}"
            print(f"Mirror source download: s3://{source_bucket}/{obj.key} -> s3://{destination_bucket}/{mirror_key}")
            s3_client.copy_object(
                Bucket=destination_bucket,
                Key=mirror_key,
                CopySource={"Bucket": source_bucket, "Key": obj.key},
                MetadataDirective="REPLACE",
                ContentType="text/csv" if filename.lower().endswith(".csv") else "application/octet-stream",
                ContentDisposition=f'attachment; filename="{filename}"',
                CacheControl="no-cache",
            )
            url = public_url(public_base_url, mirror_relative)
            s3_uri = f"s3://{destination_bucket}/{mirror_key}"
        else:
            url = s3_client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": source_bucket,
                    "Key": obj.key,
                    "ResponseContentDisposition": f'attachment; filename="{filename}"',
                },
                ExpiresIn=MISSION_DOWNLOAD_EXPIRES_SECONDS,
            )
        downloads[file_id] = {
            "url": url,
            "s3_uri": s3_uri,
            "size": obj.size,
            "size_label": human_size(obj.size),
        }
    return downloads


def build_dashboard_payload(
    source_uri: str,
    objects: list[S3Object],
    cache_dir: Path,
    bucket: str,
    source_prefix: str,
    mission_downloads: dict[str, dict[str, dict]] | None = None,
    source_downloads: dict[str, dict] | None = None,
) -> dict:
    campaigns = sorted(
        {
            campaign
            for obj in objects
            if should_include_source_object(obj.key)
            for campaign in [campaign_from_relative(PurePosixPath(obj.key[len(source_prefix) :]))]
            if campaign
        },
        key=natural_sort_key,
    )
    return {
        "title": "Instrument Campaign",
        "source_s3_uri": source_uri,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "builder_version": BUILDER_VERSION,
        "kinds": KIND_ORDER,
        "campaigns": [
            build_campaign_payload(
                campaign,
                source_prefix,
                objects,
                cache_dir,
                bucket,
                mission_downloads or {},
                source_downloads or {},
            )
            for campaign in campaigns
        ],
    }


def mission_page_relative_path(campaign_id: str, mission_id: str) -> str:
    return f"missions/{safe_download_name(campaign_id)}/{safe_download_name(mission_id)}.html"


def group_counts_from_records(groups: dict[str, dict], mission_id: str | None = None) -> dict[str, int]:
    counts = empty_group_counts()
    for kind in KIND_ORDER:
        records = groups[kind]["files"]
        if mission_id is not None:
            records = [record for record in records if record["mission"] == mission_id]
        counts[kind] = len(records)
    return counts


def mission_summary_for_index(campaign: dict, mission: dict) -> dict:
    summary = {
        "id": mission["id"],
        "name": mission["name"],
        "file_count": mission.get("file_count", 0),
        "groups": mission.get("groups", empty_group_counts()),
        "page_url": mission_page_relative_path(campaign["id"], mission["id"]),
    }
    for key in ("download_url", "download_size_label", "download_file_count", "download_s3_uri"):
        if key in mission:
            summary[key] = mission[key]
    return summary


def build_index_payload(payload: dict) -> dict:
    campaigns = []
    for campaign in payload["campaigns"]:
        campaigns.append(
            {
                "id": campaign["id"],
                "name": campaign["name"],
                "total_files": campaign["total_files"],
                "groups": group_counts_from_records(campaign["groups"]),
                "missions": [mission_summary_for_index(campaign, mission) for mission in campaign["missions"]],
            }
        )
    return {
        "title": payload["title"],
        "source_s3_uri": payload["source_s3_uri"],
        "generated": payload["generated"],
        "builder_version": payload["builder_version"],
        "campaigns": campaigns,
    }


def build_mission_payload(payload: dict, campaign_id: str, mission_id: str) -> dict:
    source_campaign = next((campaign for campaign in payload["campaigns"] if campaign["id"] == campaign_id), None)
    if not source_campaign:
        raise ValueError(f"Campaign not found: {campaign_id}")
    source_mission = next((mission for mission in source_campaign["missions"] if mission["id"] == mission_id), None)
    if not source_mission:
        raise ValueError(f"Mission not found: {campaign_id}/{mission_id}")

    groups: dict[str, dict] = {}
    series_ids: set[str] = set()
    phase_stats: dict[str, dict] = {}
    channels: dict[str, dict] = {}

    for kind in KIND_ORDER:
        source_group = source_campaign["groups"][kind]
        records = [copy.deepcopy(record) for record in source_group["files"] if record["mission"] == mission_id]
        groups[kind] = {
            "key": source_group["key"],
            "title": source_group["title"],
            "x_title": source_group["x_title"],
            "y_title": source_group["y_title"],
            "x_axis_type": source_group["x_axis_type"],
            "y_axis_type": source_group["y_axis_type"],
            "files": records,
            "selectors": selectors_for_group(kind, records),
        }
        for record in records:
            series_ids.add(record["id"])
            phase_stat = phase_stats.setdefault(
                record["phase"],
                {"id": record["phase"], "name": record["phase"], "file_count": 0, "groups": empty_group_counts()},
            )
            phase_stat["file_count"] += 1
            phase_stat["groups"][kind] += 1

            if kind not in {"TAS", "STRAIN"}:
                parsed = source_campaign["series"].get(record["id"], {})
                for trace in parsed.get("traces", []):
                    channel = trace.get("channel") or ""
                    key = normalize_match_text(channel)
                    if not key:
                        continue
                    option = channels.setdefault(
                        key,
                        {
                            "id": f"exact:{channel}",
                            "name": friendly_channel_name(channel),
                            "detail": channel,
                            "token": f"exact:{channel}",
                            "count": 0,
                        },
                    )
                    option["count"] += 1

    channel_options = [
        {"id": "all", "name": "All Sensors", "detail": "No accelerometer sensor filter", "token": "all", "count": 0}
    ]
    channel_options.extend(sorted(channels.values(), key=lambda option: channel_sort_key(option["detail"])))

    mission = copy.deepcopy(source_mission)
    campaign = {
        "id": source_campaign["id"],
        "name": source_campaign["name"],
        "total_files": sum(len(groups[kind]["files"]) for kind in KIND_ORDER),
        "missions": [mission],
        "phases": sorted(phase_stats.values(), key=lambda item: flight_phase_sort_key(item["name"])),
        "groups": groups,
        "series": {file_id: source_campaign["series"][file_id] for file_id in series_ids if file_id in source_campaign["series"]},
        "channels": channel_options,
    }
    return {
        "title": payload["title"],
        "source_s3_uri": payload["source_s3_uri"],
        "generated": payload["generated"],
        "builder_version": payload["builder_version"],
        "kinds": payload["kinds"],
        "page_type": "mission",
        "home_url": "../../index.html",
        "campaigns": [campaign],
    }


def build_site_files(payload: dict) -> dict[str, str]:
    site_files: dict[str, str] = {}
    index_payload = build_index_payload(payload)
    for campaign in payload["campaigns"]:
        for mission in campaign["missions"]:
            relative_path = mission_page_relative_path(campaign["id"], mission["id"])
            mission_payload = build_mission_payload(payload, campaign["id"], mission["id"])
            site_files[relative_path] = render_html(mission_payload)
    site_files["index.html"] = render_index_html(index_payload)
    return site_files


def json_for_html(payload: dict) -> str:
    return (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def render_html(payload: dict) -> str:
    data_json = json_for_html(payload)
    return HTML_TEMPLATE.replace("__DASHBOARD_DATA__", data_json)


def render_index_html(payload: dict) -> str:
    data_json = json_for_html(payload)
    return INDEX_HTML_TEMPLATE.replace("__INDEX_DATA__", data_json)


def site_files_hash(site_files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative_path in sorted(site_files):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(site_files[relative_path].encode("utf-8")).hexdigest().encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def write_site_files(site_files: dict[str, str], output_dir: Path) -> list[Path]:
    written = []
    for relative_path, html_text in sorted(site_files.items()):
        target = output_dir / Path(*PurePosixPath(relative_path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html_text, encoding="utf-8")
        written.append(target)
    return written


def upload_if_changed(s3_client, site_files: dict[str, str], manifest: dict, destination_uri: str) -> bool:
    bucket, prefix = split_s3_uri(destination_uri)
    manifest_key = f"{prefix}dashboard_manifest.json"
    content_hash = site_files_hash(site_files)
    existing_hash = None
    try:
        response = s3_client.get_object(Bucket=bucket, Key=manifest_key)
        existing = json.loads(response["Body"].read().decode("utf-8"))
        existing_hash = existing.get("content_hash")
    except Exception:
        existing_hash = None
    if existing_hash == content_hash:
        print("No frontend upload needed; generated site files are unchanged.")
        return False
    upload_order = [relative_path for relative_path in sorted(site_files) if relative_path != "index.html"]
    if "index.html" in site_files:
        upload_order.append("index.html")
    for relative_path in upload_order:
        key = f"{prefix}{relative_path}"
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=site_files[relative_path].encode("utf-8"),
            ContentType="text/html; charset=utf-8",
            CacheControl="no-cache",
        )
        print(f"Uploaded dashboard page: s3://{bucket}/{key}")
    manifest = {
        **manifest,
        "content_hash": content_hash,
        "site_file_count": len(site_files),
        "site_files": sorted(site_files),
        "uploaded": datetime.now().isoformat(),
    }
    s3_client.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
        CacheControl="no-cache",
    )
    print(f"Uploaded dashboard manifest: s3://{bucket}/{manifest_key}")
    return True


def build_once(args: argparse.Namespace) -> int:
    source_bucket, source_prefix = split_s3_uri(args.source)
    destination_bucket, destination_prefix = split_s3_uri(args.destination)
    profile = choose_aws_profile(args.profile)
    s3_client = get_s3_client(profile)
    cache_dir = Path(args.cache_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Source: s3://{source_bucket}/{source_prefix}")
    print(f"Destination: s3://{destination_bucket}/{destination_prefix}")
    print(f"AWS profile: {profile or 'default credential chain'}")
    public_base_url = (args.public_base_url or os.environ.get("DASHBOARD_PUBLIC_BASE_URL") or PUBLIC_BASE_URL).strip().rstrip("/") or None
    if public_base_url:
        print(f"Public base URL: {public_base_url}")
    objects = list_s3_objects(s3_client, source_bucket, source_prefix)
    source_objects = [obj for obj in objects if should_include_source_object(obj.key)]
    manifest_hash = source_manifest_hash(source_objects)
    existing_manifest = get_existing_manifest(s3_client, destination_bucket, destination_prefix)
    if (
        existing_manifest
        and existing_manifest.get("source_manifest_hash") == manifest_hash
        and not args.force
    ):
        print("No new source data found. Skipping dashboard rebuild and upload.")
        return 0

    print(f"Source files to consider: {len(source_objects)}")
    download_changed_objects(s3_client, source_bucket, source_objects, cache_dir)
    mission_downloads = {}
    source_downloads = {}
    if not args.no_upload:
        mission_downloads = build_mission_downloads(
            s3_client,
            source_bucket,
            source_prefix,
            source_objects,
            cache_dir,
            destination_bucket,
            destination_prefix,
            public_base_url,
        )
        source_downloads = build_source_downloads(
            s3_client,
            source_bucket,
            source_prefix,
            source_objects,
            destination_bucket,
            destination_prefix,
            public_base_url,
        )
    payload = build_dashboard_payload(
        args.source,
        source_objects,
        cache_dir,
        source_bucket,
        source_prefix,
        mission_downloads,
        source_downloads,
    )
    site_files = build_site_files(payload)
    written_files = write_site_files(site_files, output_dir)
    index_file = output_dir / "index.html"
    print(
        f"Wrote local dashboard: {index_file} ({human_size(index_file.stat().st_size)}) "
        f"and {len(written_files) - 1} mission pages"
    )

    manifest = {
        "source_uri": args.source,
        "destination_uri": args.destination,
        "source_manifest_hash": manifest_hash,
        "source_file_count": len(source_objects),
        "campaigns": [campaign["id"] for campaign in payload["campaigns"]],
        "builder_version": BUILDER_VERSION,
        "generated": payload["generated"],
    }
    if args.no_upload:
        print("No upload requested.")
        return 0
    upload_if_changed(s3_client, site_files, manifest, args.destination)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the static instrument flight test dashboard.")
    parser.add_argument("--source", default=SOURCE_S3_URI)
    parser.add_argument("--destination", default=DESTINATION_S3_URI)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--cache-dir", default=str(LOCAL_CACHE_DIR))
    parser.add_argument("--output-dir", default=str(LOCAL_BUILD_DIR))
    parser.add_argument("--public-base-url", default=None, help="Stable public URL that serves the destination prefix, for example https://example.cloudfront.net")
    parser.add_argument("--force", action="store_true", help="Rebuild even when the source manifest has not changed.")
    parser.add_argument("--no-upload", action="store_true", help="Only write the local index.html.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return build_once(args)


INDEX_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Instrument Campaign</title>
  <style>
* { box-sizing: border-box; }
body {
  margin: 0;
  background: #f4f6fa;
  color: #111827;
  font-family: Arial, Helvetica, sans-serif;
  font-size: 12px;
  letter-spacing: 0;
}
main { width: 100%; max-width: none; margin: 0; padding: 18px 16px 34px; }
h1 { margin: 0 0 6px; color: #000; font-size: 20px; font-weight: 700; line-height: 1.25; }
button, input {
  border: 1px solid #c7d2e4;
  border-radius: 6px;
  background: #fff;
  color: #111827;
  font: inherit;
}
button { cursor: pointer; }
button:hover { background: #f5f7fb; }
input { min-height: 32px; padding: 0 9px; }
.page-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
  margin-bottom: 12px;
}
.meta { display: grid; gap: 3px; color: #334155; line-height: 1.35; overflow-wrap: anywhere; }
.status-line {
  border: 1px solid #b9d8c4;
  border-radius: 6px;
  background: #f3fbf6;
  margin-bottom: 10px;
  padding: 8px 10px;
  color: #1d5d35;
  line-height: 1.35;
}
.filter-block {
  display: grid;
  grid-template-columns: 170px minmax(0, 1fr);
  gap: 8px;
  align-items: stretch;
  margin: 0 0 10px;
  border: 1px solid #d6dce8;
  border-radius: 6px;
  background: #fff;
  padding: 8px;
}
.filter-head {
  display: grid;
  align-content: center;
  gap: 2px;
  min-width: 150px;
  padding: 4px 8px;
  border-right: 1px solid #d6dce8;
  color: #111827;
  font-weight: 700;
}
.filter-head small {
  color: #475569;
  font-size: 10px;
  font-weight: 700;
  line-height: 1.2;
}
.filter-body { min-width: 0; display: grid; gap: 7px; }
.button-row { display: flex; gap: 7px; overflow-x: auto; padding-bottom: 1px; }
.mission-choice {
  display: flex;
  gap: 6px;
  align-items: stretch;
  flex: 0 0 auto;
}
.mission-choice .choice-button { min-width: 240px; }
.choice-button {
  display: grid;
  gap: 2px;
  min-width: 134px;
  max-width: 260px;
  min-height: 38px;
  flex: 0 0 auto;
  padding: 6px 9px;
  border: 1px solid #c7d2e4;
  border-radius: 6px;
  background: #fff;
  color: #111827;
  text-align: left;
  text-decoration: none;
}
.choice-button:hover { background: #f5f7fb; }
.choice-button.active {
  background: #0f766e;
  border-color: #0f766e;
  color: #fff;
}
.choice-button.summary {
  cursor: default;
}
.choice-button.summary:hover {
  background: #fff;
}
.choice-button span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.choice-button small {
  display: block;
  overflow: hidden;
  color: inherit;
  font-size: 10px;
  line-height: 1.2;
  opacity: 0.78;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.download-button {
  display: grid;
  align-content: center;
  min-width: 88px;
  padding: 7px 10px;
  border: 1px solid #c7d2e4;
  border-radius: 6px;
  background: #fff;
  color: #111827;
  text-decoration: none;
  line-height: 1.2;
}
.download-button:hover { background: #f5f7fb; }
.download-button span { font-weight: 700; }
.download-button small { display: block; margin-top: 2px; color: #64748b; white-space: nowrap; }
.download-button.disabled {
  pointer-events: none;
  opacity: 0.45;
}
.empty-state {
  border: 1px solid #ead7a4;
  border-radius: 6px;
  background: #fffbeb;
  color: #6f4f00;
  padding: 9px 10px;
}
@media (max-width: 900px) {
  main { padding: 14px 10px 26px; }
  .page-head { display: grid; }
  .filter-block { grid-template-columns: 1fr; }
  .filter-head { border-right: 0; border-bottom: 1px solid #d6dce8; }
  .mission-choice { display: grid; min-width: 260px; }
  .mission-choice .choice-button, .download-button { min-width: 0; }
}
  </style>
</head>
<body>
  <main>
    <header class="page-head">
      <div>
        <h1>Instrument Campaign</h1>
        <div class="meta">
          <div><strong>S3 source:</strong> <span id="s3-source"></span></div>
          <div><strong>Generated:</strong> <span id="generated"></span></div>
          <div><strong>Current view:</strong> <span id="current-view"></span></div>
        </div>
      </div>
    </header>
    <div id="status-line" class="status-line">Starting...</div>
    <div id="campaign-filter" class="filter-block"></div>
    <div id="mission-filter" class="filter-block"></div>
  </main>
  <script id="index-data" type="application/json">__INDEX_DATA__</script>
  <script>
const payload = JSON.parse(document.getElementById("index-data").textContent);
const KINDS = ["TAS", "ERS", "FDS", "PSD", "STRAIN"];
let campaignId = payload.campaigns[0]?.id || "";
let missionSearch = "";
const elements = {
  status: document.getElementById("status-line"),
  currentView: document.getElementById("current-view"),
  s3: document.getElementById("s3-source"),
  generated: document.getElementById("generated"),
  campaign: document.getElementById("campaign-filter"),
  mission: document.getElementById("mission-filter"),
};
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
function currentCampaign() {
  return payload.campaigns.find((item) => item.id === campaignId) || payload.campaigns[0];
}
function groupDetail(groups) {
  return `TAS ${groups.TAS || 0} | ERS ${groups.ERS || 0} | FDS ${groups.FDS || 0} | PSD ${groups.PSD || 0} | Strain ${groups.STRAIN || 0}`;
}
function choiceButton(option, active, attr) {
  return `<button class="choice-button ${active ? "active" : ""}" type="button" ${attr}="${escapeHtml(option.id)}">
    <span>${escapeHtml(option.name)}</span>
    <small>${escapeHtml(option.detail || "")}</small>
  </button>`;
}
function missionChoice(mission) {
  const detail = groupDetail(mission.groups || {});
  const link = `<a class="choice-button" href="${escapeHtml(mission.page_url)}" title="Open ${escapeHtml(mission.name)} dashboard">
    <span>${escapeHtml(mission.name)}</span>
    <small>${escapeHtml(detail)}</small>
  </a>`;
  const download = mission.download_url
    ? `<a class="download-button" href="${escapeHtml(mission.download_url)}" target="_blank" rel="noopener" download title="Download ${escapeHtml(mission.name)} mission files from AWS">
        <span>Download</span>
        <small>${escapeHtml(mission.download_size_label || "")}</small>
      </a>`
    : `<span class="download-button disabled" title="Download will be available after the dashboard uploads">
        <span>Download</span>
        <small>Preparing</small>
      </span>`;
  return `<div class="mission-choice">${link}${download}</div>`;
}
function buildCampaignFilter() {
  if (!payload.campaigns.length) {
    elements.campaign.innerHTML = `<div class="empty-state">No zip folders were found in the source bucket.</div>`;
    return;
  }
  elements.campaign.innerHTML = `<div class="filter-head"><span>Zip Number</span><small>${escapeHtml(campaignId)}</small></div>
    <div class="filter-body"><div class="button-row">
      ${payload.campaigns.map((campaign) => {
        const detail = groupDetail(campaign.groups || {});
        return choiceButton({ id: campaign.id, name: campaign.name, detail }, campaign.id === campaignId, "data-campaign");
      }).join("")}
    </div></div>`;
  elements.campaign.querySelectorAll("[data-campaign]").forEach((button) => {
    button.addEventListener("click", () => {
      campaignId = button.dataset.campaign;
      missionSearch = "";
      rebuild();
    });
  });
}
function buildMissionFilter() {
  const campaign = currentCampaign();
  if (!campaign) {
    elements.mission.innerHTML = "";
    return;
  }
  const query = missionSearch.trim().toLowerCase();
  const missions = (campaign.missions || []).filter((mission) => !query || `${mission.name} ${mission.id}`.toLowerCase().includes(query));
  const allSummary = `<span class="choice-button summary">
    <span>All Missions</span>
    <small>${escapeHtml(`${campaign.total_files || 0} files`)}</small>
  </span>`;
  elements.mission.innerHTML = `<div class="filter-head"><span>Mission ID</span><small>${escapeHtml(query ? "Search" : "Select a mission")}</small></div>
    <div class="filter-body">
      <input id="mission-search" type="search" placeholder="Search mission ID" value="${escapeHtml(missionSearch)}">
      <div class="button-row">
        ${allSummary}
        ${missions.map((mission) => missionChoice(mission)).join("")}
      </div>
    </div>`;
  const search = document.getElementById("mission-search");
  search.addEventListener("input", () => {
    missionSearch = search.value;
    buildMissionFilter();
    updateStatus();
  });
}
function updateStatus() {
  const campaign = currentCampaign();
  const missionCount = campaign?.missions?.length || 0;
  const matchCount = campaign ? (campaign.missions || []).filter((mission) => {
    const query = missionSearch.trim().toLowerCase();
    return !query || `${mission.name} ${mission.id}`.toLowerCase().includes(query);
  }).length : 0;
  elements.status.textContent = campaign
    ? `Ready. Zip ${campaign.id} has ${missionCount} missions. ${matchCount} mission${matchCount === 1 ? "" : "s"} shown.`
    : "No campaign data found.";
  elements.currentView.textContent = campaign ? `Zip ${campaign.id} | Select a mission` : "No campaign selected";
}
function rebuild() {
  buildCampaignFilter();
  buildMissionFilter();
  updateStatus();
}
function initialize() {
  elements.s3.textContent = payload.source_s3_uri || "";
  elements.generated.textContent = payload.generated || "";
  rebuild();
}
initialize();
  </script>
</body>
</html>
"""


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Instrument Campaign</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
* { box-sizing: border-box; }
body {
  margin: 0;
  background: #f4f6fa;
  color: #111827;
  font-family: Arial, Helvetica, sans-serif;
  font-size: 12px;
  letter-spacing: 0;
}
main { width: 100%; max-width: none; margin: 0; padding: 18px 16px 34px; }
h1 { margin: 0 0 6px; color: #000; font-size: 20px; font-weight: 700; line-height: 1.25; }
button, input {
  border: 1px solid #c7d2e4;
  border-radius: 6px;
  background: #fff;
  color: #111827;
  font: inherit;
}
button { cursor: pointer; }
button:hover { background: #f5f7fb; }
input { min-height: 32px; padding: 0 9px; }
.page-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
  margin-bottom: 12px;
}
.meta { display: grid; gap: 3px; color: #334155; line-height: 1.35; overflow-wrap: anywhere; }
.back-link {
  display: inline-flex;
  align-items: center;
  margin: 0 0 8px;
  color: #0f766e;
  font-weight: 700;
  text-decoration: none;
}
.back-link:hover { text-decoration: underline; }
.status-line {
  border: 1px solid #b9d8c4;
  border-radius: 6px;
  background: #f3fbf6;
  margin-bottom: 10px;
  padding: 8px 10px;
  color: #1d5d35;
  line-height: 1.35;
}
.filter-block {
  display: grid;
  grid-template-columns: 170px minmax(0, 1fr);
  gap: 8px;
  align-items: stretch;
  margin: 0 0 10px;
  border: 1px solid #d6dce8;
  border-radius: 6px;
  background: #fff;
  padding: 8px;
}
.mission-page-hidden { display: none; }
.filter-head {
  display: grid;
  align-content: center;
  gap: 2px;
  min-width: 150px;
  padding: 4px 8px;
  border-right: 1px solid #d6dce8;
  color: #111827;
  font-weight: 700;
}
.filter-head small {
  color: #475569;
  font-size: 10px;
  font-weight: 700;
  line-height: 1.2;
}
.filter-body { min-width: 0; display: grid; gap: 7px; }
.button-row { display: flex; gap: 7px; overflow-x: auto; padding-bottom: 1px; }
.mission-choice {
  display: flex;
  gap: 6px;
  align-items: stretch;
  flex: 0 0 auto;
}
.mission-choice .choice-button { min-width: 178px; }
.download-button {
  display: grid;
  align-content: center;
  min-width: 88px;
  padding: 7px 10px;
  border: 1px solid #c7d2e4;
  border-radius: 6px;
  background: #fff;
  color: #111827;
  text-decoration: none;
  line-height: 1.2;
}
.download-button:hover { background: #f5f7fb; }
.download-button span { font-weight: 700; }
.download-button small { display: block; margin-top: 2px; color: #64748b; white-space: nowrap; }
.download-button.disabled {
  pointer-events: none;
  opacity: 0.45;
}
.choice-button {
  display: grid;
  gap: 2px;
  min-width: 134px;
  max-width: 230px;
  min-height: 38px;
  flex: 0 0 auto;
  padding: 6px 9px;
  text-align: left;
}
.choice-button.active {
  background: #0f766e;
  border-color: #0f766e;
  color: #fff;
}
.choice-button.phase.active {
  background: #111827;
  border-color: #111827;
}
.choice-button.phase-toggle {
  border-color: #9fb4d2;
}
.choice-button.phase-toggle.off {
  opacity: 0.52;
  background: #f8fafc;
}
.choice-button.phase-toggle.off span {
  text-decoration: line-through;
}
.choice-button small {
  display: block;
  overflow: hidden;
  color: inherit;
  font-size: 10px;
  line-height: 1.2;
  opacity: 0.78;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.strain-tool {
  display: grid;
  grid-template-columns: 170px minmax(160px, 240px) minmax(0, 1fr);
  gap: 10px;
  align-items: center;
  margin: 0 0 14px;
  border: 1px solid #d6dce8;
  border-radius: 6px;
  background: #fff;
  padding: 9px;
}
.strain-tool label { font-weight: 700; }
.strain-tool .formula {
  color: #475569;
  display: grid;
  gap: 3px;
  line-height: 1.35;
}
.strain-tool .formula-equation {
  color: #0f172a;
  font-weight: 700;
}
.sections { display: grid; gap: 14px; }
.comparison-section {
  background: #fff;
  border: 1px solid #cfd7e6;
  border-radius: 6px;
  padding: 12px;
  box-shadow: 0 6px 16px rgba(15, 23, 42, 0.06);
}
.section-title-row {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  align-items: center;
  margin-bottom: 10px;
}
.section-title { margin: 0; color: #000; font-size: 13px; font-weight: 700; }
.section-count { color: #475569; font-size: 11px; font-weight: 700; white-space: nowrap; }
.comparison-grid {
  display: grid;
  grid-template-columns: 320px minmax(0, 1fr);
  gap: 12px;
  align-items: start;
}
.rank-panel {
  border: 1px solid #d6dce8;
  border-radius: 6px;
  background: #fbfcff;
  overflow: hidden;
}
.rank-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 9px;
  border-bottom: 1px solid #d6dce8;
  background: #eef2f8;
  color: #000;
  font-weight: 700;
}
.search-input {
  width: 100%;
  border: 0;
  border-bottom: 1px solid #d6dce8;
  border-radius: 0;
  padding: 8px 9px;
}
.rank-tools {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  border-bottom: 1px solid #d6dce8;
  background: #f8fafc;
  padding: 7px 8px;
}
.rank-tools button { min-height: 28px; padding: 0 8px; font-weight: 700; }
.rank-list { display: grid; max-height: 4.5in; overflow: auto; }
.rank-row {
  width: 100%;
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr) 70px;
  align-items: start;
  border-bottom: 1px solid #e3e8f2;
  background: #fff;
}
.rank-row.active { background: #e8eef8; box-shadow: inset 3px 0 0 #111827; }
.rank-check, .rank-check-spacer {
  display: flex;
  align-items: flex-start;
  justify-content: center;
  min-height: 42px;
  padding-top: 9px;
}
.trace-toggle { width: 15px; height: 15px; accent-color: #111827; cursor: pointer; }
.rank-button {
  width: 100%;
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr);
  gap: 7px;
  align-items: start;
  border: 0;
  border-radius: 0;
  background: transparent;
  padding: 7px 9px;
  text-align: left;
}
.rank-download-cell {
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 7px 7px 7px 0;
}
.rank-download-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 24px;
  width: 62px;
  border: 1px solid #c7d2e4;
  border-radius: 5px;
  background: #fff;
  color: #111827;
  font-size: 10px;
  font-weight: 700;
  line-height: 1;
  text-decoration: none;
}
.rank-download-button:hover { background: #f5f7fb; }
.rank-download-button.disabled {
  pointer-events: none;
  opacity: 0.45;
}
.rank-number { color: #475569; font-size: 11px; font-weight: 700; }
.rank-label {
  display: block;
  color: #111827;
  font-weight: 700;
  line-height: 1.25;
  overflow-wrap: anywhere;
}
.rank-full-name {
  display: block;
  margin-top: 3px;
  color: #334155;
  font-size: 10.5px;
  font-weight: 600;
  line-height: 1.25;
  overflow-wrap: anywhere;
}
.rank-value {
  display: block;
  margin-top: 2px;
  color: #475569;
  font-size: 11px;
  line-height: 1.25;
}
.plot-wrap { min-width: 0; }
.plotly-chart { width: 100%; height: 4.6in; }
.warning-list {
  display: none;
  margin-top: 8px;
  border: 1px solid #ead7a4;
  border-radius: 6px;
  background: #fffbeb;
  color: #6f4f00;
  line-height: 1.35;
  overflow: hidden;
}
.warning-list.visible { display: block; }
.warning-list.expanded .warning-body { display: block; }
.warning-toggle {
  width: 100%;
  display: flex;
  justify-content: space-between;
  gap: 10px;
  align-items: center;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: inherit;
  padding: 7px 10px;
  text-align: left;
  font-weight: 700;
}
.warning-toggle:hover { background: #fff4cf; }
.warning-summary {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.warning-action { flex: 0 0 auto; color: #8a6411; }
.warning-body {
  display: none;
  max-height: 112px;
  overflow: auto;
  padding: 0 10px 8px;
}
.warning-item { margin-top: 3px; }
@media (max-width: 900px) {
  main { padding: 14px 10px 26px; }
  .page-head { display: grid; }
  .filter-block, .strain-tool { grid-template-columns: 1fr; }
  .filter-head { border-right: 0; border-bottom: 1px solid #d6dce8; }
  .comparison-grid { grid-template-columns: 1fr; }
  .rank-list { max-height: 240px; }
}
  </style>
</head>
<body>
  <main>
    <header class="page-head">
      <div>
        <a class="back-link" href="../../index.html">Back to mission selector</a>
        <h1>Instrument Campaign</h1>
        <div class="meta">
          <div><strong>S3 source:</strong> <span id="s3-source"></span></div>
          <div><strong>Generated:</strong> <span id="generated"></span></div>
          <div><strong>Current view:</strong> <span id="current-view"></span></div>
        </div>
      </div>
    </header>
    <div id="status-line" class="status-line">Starting...</div>
    <div id="campaign-filter" class="filter-block mission-page-hidden"></div>
    <div id="mission-filter" class="filter-block mission-page-hidden"></div>
    <div id="phase-filter" class="filter-block"></div>
    <div id="frequency-channel-filter" class="filter-block"></div>
    <div class="strain-tool">
      <label for="strain-scale">Conversion scale gauge factor</label>
      <input id="strain-scale" type="number" step="any" value="1">
      <div class="formula">
        <div class="formula-equation">PSD<sub>strain (unit: Microstrain)</sub> = PSD<sub>voltage (unit: mV)</sub> * CF<sup>2</sup>; strain<sub>unit micro strain</sub> = CF * Voltage<sub>unit mV</sub></div>
      </div>
    </div>
    <div id="sections" class="sections"></div>
  </main>
  <script id="dashboard-data" type="application/json">__DASHBOARD_DATA__</script>
  <script>
const payload = JSON.parse(document.getElementById("dashboard-data").textContent);
const KINDS = payload.kinds;
const palette = ["#111827", "#2563eb", "#c2410c", "#0f766e", "#7c3aed", "#be123c", "#4d7c0f", "#0369a1"];
const frequencyTickVals = [10,20,30,40,50,60,70,80,90,100,200,300,400,500,600,700,800,900,1000];
const frequencyTickText = ["10","2","3","4","5","6","7","8","9","100","2","3","4","5","6","7","8","9","1000"];
let campaignId = payload.campaigns[0]?.id || "all";
let missionFilter = "all";
let missionSearch = "";
let activePhase = "all";
let frequencyChannel = "all";
let strainScale = 1;
const state = Object.fromEntries(KINDS.map((kind) => [kind, "all"]));
const visibleState = Object.fromEntries(KINDS.map((kind) => [kind, new Set(["all"])]));
const warningExpanded = Object.fromEntries(KINDS.map((kind) => [kind, false]));

const elements = {
  status: document.getElementById("status-line"),
  currentView: document.getElementById("current-view"),
  s3: document.getElementById("s3-source"),
  generated: document.getElementById("generated"),
  campaign: document.getElementById("campaign-filter"),
  mission: document.getElementById("mission-filter"),
  phase: document.getElementById("phase-filter"),
  frequencyChannel: document.getElementById("frequency-channel-filter"),
  sections: document.getElementById("sections"),
  strainScale: document.getElementById("strain-scale"),
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
function currentCampaign() {
  return payload.campaigns.find((item) => item.id === campaignId) || payload.campaigns[0];
}
function emptyCounts() {
  return Object.fromEntries(KINDS.map((kind) => [kind, 0]));
}
function missionMatches(record) {
  return missionFilter === "all" || record.mission === missionFilter || record.mission === "all";
}
function phaseScopeFor(kind) {
  return activePhase;
}
function phaseMatches(kind, record) {
  const scope = phaseScopeFor(kind);
  return scope === "all" || record.phase === scope || record.phase === "all";
}
function recordsFor(kind) {
  const campaign = currentCampaign();
  return (campaign.groups[kind].files || []).filter((record) => missionMatches(record) && phaseMatches(kind, record));
}
function selectorsFor(kind) {
  const campaign = currentCampaign();
  return (campaign.groups[kind].selectors || []).filter((record) => missionMatches(record) && (record.type === "all" || phaseMatches(kind, record)));
}
function traceAllowedForKind(kind, trace) {
  return kind === "TAS" || kind === "STRAIN" || traceMatchesChannel(trace);
}
function traceOptionId(record, traceIndex) {
  return `${record.id}::trace::${traceIndex}`;
}
function traceSelectorOptionsFor(kind) {
  const options = [{ id: "all", type: "all", number: "All", name: `${kind} (All)`, detail: "Show matching graph lines", phase: "all", mission: "all" }];
  let graphNumber = 1;
  for (const record of recordsFor(kind)) {
    const parsed = currentCampaign().series[record.id];
    if (!parsed) continue;
    (parsed.traces || []).forEach((trace, traceIndex) => {
      if (!traceAllowedForKind(kind, trace)) return;
      const label = compactLegendName(trace.name);
      options.push({
        id: traceOptionId(record, traceIndex),
        type: "trace",
        number: `Graph ${graphNumber}`,
        name: label,
        detail: `${record.mission} | ${record.phase} | ${trace.channel || "Channel"}`,
        file_name: trace.name,
        source_name: record.name,
        phase: record.phase,
        mission: record.mission,
        record_id: record.id,
        trace_index: traceIndex,
        channel: trace.channel || "",
        download_url: record.download_url || "",
        download_s3_uri: record.download_s3_uri || "",
      });
      graphNumber += 1;
    });
  }
  return options;
}
function selectionOptionsFor(kind) {
  return kind === "TAS" ? selectorsFor(kind) : traceSelectorOptionsFor(kind);
}
function getVisibleSet(kind) {
  if (!visibleState[kind]) visibleState[kind] = new Set(["all"]);
  return visibleState[kind];
}
function setVisibleSet(kind, ids) {
  visibleState[kind] = new Set(ids);
}
function resetSelections(allKinds = KINDS) {
  for (const kind of allKinds) {
    state[kind] = "all";
    setVisibleSet(kind, ["all"]);
  }
}
function campaignCounts(campaign) {
  const groups = emptyCounts();
  for (const kind of KINDS) groups[kind] = campaign.groups[kind].files.length;
  return groups;
}
function missionCountsFor(campaign, missionId) {
  const groups = emptyCounts();
  for (const kind of KINDS) {
    groups[kind] = campaign.groups[kind].files.filter((record) => missionId === "all" || record.mission === missionId).length;
  }
  return groups;
}
function phaseSummariesForMission() {
  const campaign = currentCampaign();
  const summaries = new Map();
  for (const phase of campaign.phases || []) {
    summaries.set(phase.id, { id: phase.id, name: phase.name, file_count: 0, groups: emptyCounts() });
  }
  for (const kind of KINDS) {
    for (const record of campaign.groups[kind].files || []) {
      if (!missionMatches(record)) continue;
      if (!summaries.has(record.phase)) {
        summaries.set(record.phase, { id: record.phase, name: record.phase, file_count: 0, groups: emptyCounts() });
      }
      const item = summaries.get(record.phase);
      item.file_count += 1;
      item.groups[kind] += 1;
    }
  }
  const phases = Array.from(summaries.values());
  return missionFilter === "all" ? phases : phases.filter((phase) => phase.file_count > 0);
}
function phaseFileCount() {
  return phaseSummariesForMission().reduce((total, phase) => total + phase.file_count, 0);
}
function strainPhaseSummariesForMission() {
  return phaseSummariesForMission().filter((phase) => {
    if ((phase.groups?.STRAIN || 0) === 0) return false;
    return activePhase === "all" || phase.id === activePhase;
  });
}
function visibleStrainPhaseSummary() {
  const phases = strainPhaseSummariesForMission();
  return activePhase === "all" ? `${phases.length} strain phases` : `${activePhase} strain phase`;
}
function sectionHtml(kind) {
  const group = currentCampaign().groups[kind];
  const selectorCount = selectionOptionsFor(kind).filter((item) => item.type !== "all").length;
  return `
    <section class="comparison-section" id="${kind}-section">
      <div class="section-title-row">
        <h2 class="section-title">${escapeHtml(group.title)}</h2>
        <span class="section-count" id="${kind}-count">${selectorCount} selections</span>
      </div>
      <div class="comparison-grid">
        <aside class="rank-panel">
          <div class="rank-head"><span>${kind} Selections</span><span>${selectorCount}</span></div>
          <input class="search-input" id="${kind}-search" type="search" placeholder="Filter ${kind}">
          <div class="rank-tools">
            <button type="button" id="${kind}-show-all">Show All</button>
            <button type="button" id="${kind}-hide-all">Hide All</button>
          </div>
          <div class="rank-list" id="${kind}-list"></div>
        </aside>
        <div class="plot-wrap">
          <div class="plotly-chart" id="${kind}-plot"></div>
          <div class="warning-list" id="${kind}-warnings"></div>
        </div>
      </div>
    </section>`;
}
function choiceButton(option, active, attr, extraClass = "") {
  return `<button class="choice-button ${extraClass} ${active ? "active" : ""}" type="button" ${attr}="${escapeHtml(option.id)}">
    <span>${escapeHtml(option.name)}</span>
    <small>${escapeHtml(option.detail || "")}</small>
  </button>`;
}
function missionChoice(mission, detail) {
  const selectButton = choiceButton({ id: mission.id, name: mission.name, detail }, mission.id === missionFilter, "data-mission");
  const download = mission.download_url
    ? `<a class="download-button" href="${escapeHtml(mission.download_url)}" target="_blank" rel="noopener" download title="Download ${escapeHtml(mission.name)} mission files from AWS">
        <span>Download</span>
        <small>${escapeHtml(mission.download_size_label || "")}</small>
      </a>`
    : `<span class="download-button disabled" title="Download will be available after the dashboard uploads">
        <span>Download</span>
        <small>Preparing</small>
      </span>`;
  return `<div class="mission-choice">${selectButton}${download}</div>`;
}
function buildCampaignFilter() {
  elements.campaign.innerHTML = `<div class="filter-head"><span>Zip Number</span><small>${escapeHtml(campaignId)}</small></div>
    <div class="filter-body"><div class="button-row">
      ${payload.campaigns.map((campaign) => {
        const counts = campaignCounts(campaign);
        const detail = `TAS ${counts.TAS} | ERS ${counts.ERS} | FDS ${counts.FDS} | PSD ${counts.PSD} | Strain ${counts.STRAIN}`;
        return choiceButton({ id: campaign.id, name: campaign.name, detail }, campaign.id === campaignId, "data-campaign");
      }).join("")}
    </div></div>`;
  elements.campaign.querySelectorAll("[data-campaign]").forEach((button) => {
    button.addEventListener("click", () => {
      campaignId = button.dataset.campaign;
      const campaign = currentCampaign();
      missionFilter = campaign.missions[0]?.id || "all";
      missionSearch = "";
      activePhase = "all";
      frequencyChannel = "all";
      resetSelections();
      rebuildAll();
    });
  });
}
function buildMissionFilter() {
  const campaign = currentCampaign();
  if (!campaign.missions.some((mission) => mission.id === missionFilter)) {
    missionFilter = campaign.missions[0]?.id || "all";
  }
  const allCounts = missionCountsFor(campaign, "all");
  const missions = [{ id: "all", name: "All Missions", detail: `${campaign.total_files} files`, groups: allCounts }, ...campaign.missions];
  const filtered = missions.filter((mission) => {
    if (mission.id === "all") return true;
    const query = missionSearch.trim().toLowerCase();
    return !query || `${mission.name} ${mission.id}`.toLowerCase().includes(query);
  });
  elements.mission.innerHTML = `<div class="filter-head"><span>Mission ID</span><small>${missionFilter === "all" ? "All Missions" : missionFilter}</small></div>
    <div class="filter-body">
      <input id="mission-search" type="search" placeholder="Search mission ID" value="${escapeHtml(missionSearch)}">
      <div class="button-row">
        ${filtered.map((mission) => {
          const groups = mission.groups || missionCountsFor(campaign, mission.id);
          const detail = mission.id === "all" ? `${campaign.total_files} files` : `TAS ${groups.TAS || 0} | ERS ${groups.ERS || 0} | FDS ${groups.FDS || 0} | PSD ${groups.PSD || 0} | Strain ${groups.STRAIN || 0}`;
          return mission.id === "all" ? choiceButton({ id: mission.id, name: mission.name, detail }, mission.id === missionFilter, "data-mission") : missionChoice(mission, detail);
        }).join("")}
      </div>
    </div>`;
  const search = document.getElementById("mission-search");
  search.addEventListener("input", () => {
    missionSearch = search.value;
    buildMissionFilter();
  });
  elements.mission.querySelectorAll("[data-mission]").forEach((button) => {
    button.addEventListener("click", () => {
      missionFilter = button.dataset.mission;
      activePhase = "all";
      frequencyChannel = "all";
      resetSelections();
      rebuildAll();
    });
  });
}
function buildPhaseFilter() {
  const phases = [{ id: "all", name: "All Phases", detail: `TAS ${missionCountsFor(currentCampaign(), missionFilter).TAS || 0}` }, ...phaseSummariesForMission().map((phase) => {
    const groups = phase.groups || {};
    return { id: phase.id, name: phase.name, detail: `TAS ${groups.TAS || 0}` };
  })];
  elements.phase.innerHTML = `<div class="filter-head"><span>TAS</span><small>${activePhase === "all" ? "All Phases" : activePhase}</small></div>
    <div class="filter-body"><div class="button-row">
      ${phases.map((phase) => choiceButton(phase, phase.id === activePhase, "data-phase", "phase")).join("")}
    </div></div>`;
  elements.phase.querySelectorAll("[data-phase]").forEach((button) => {
    button.addEventListener("click", () => {
      activePhase = button.dataset.phase;
      resetSelections();
      rebuildAll();
    });
  });
}
function channelOptionsForScope() {
  const campaign = currentCampaign();
  const channels = new Map();
  for (const option of campaign.channels || []) {
    if (option.id === "all") channels.set(option.id, option);
  }
  for (const kind of KINDS.filter((item) => item !== "TAS" && item !== "STRAIN")) {
    for (const record of recordsFor(kind)) {
      const parsed = campaign.series[record.id];
      for (const trace of parsed?.traces || []) {
        const id = `exact:${trace.channel}`;
        if (!channels.has(id)) {
          const found = (campaign.channels || []).find((item) => item.id === id);
          channels.set(id, found || { id, name: trace.channel, detail: trace.channel, token: id });
        }
      }
    }
  }
  return Array.from(channels.values());
}
function buildFrequencyChannelFilter() {
  const options = channelOptionsForScope();
  if (!options.some((option) => option.id === frequencyChannel)) frequencyChannel = "all";
  const active = options.find((option) => option.id === frequencyChannel) || options[0];
  elements.frequencyChannel.innerHTML = `<div class="filter-head"><span>Accelerometer Sensor Selection</span><small>${escapeHtml(active?.name || "All Sensors")}</small></div>
    <div class="filter-body"><div class="button-row">
      ${options.map((option) => choiceButton(option, option.id === frequencyChannel, "data-frequency-channel")).join("")}
    </div></div>`;
  elements.frequencyChannel.querySelectorAll("[data-frequency-channel]").forEach((button) => {
    button.addEventListener("click", () => {
      frequencyChannel = button.dataset.frequencyChannel;
      resetSelections(KINDS.filter((item) => item !== "TAS" && item !== "STRAIN"));
      buildFrequencyChannelFilter();
      for (const kind of KINDS.filter((item) => item !== "TAS" && item !== "STRAIN")) {
        renderFileList(kind);
        renderChart(kind);
      }
      updateStatus();
    });
  });
}
function compactSource(name, phase) {
  let source = String(name || "")
    .replace(/\.(csv|xmh)$/i, "")
    .replace(/^.*?_TSfilt_/, "")
    .replace(/_(ERS|FDS|PSD|PSD_STRAIN)$/i, "")
    .replace(/TAS$/i, "");
  if (phase && phase !== "all" && source.startsWith(`${phase}_`)) source = source.slice(phase.length + 1);
  return source.replace(/_/g, " ").trim() || String(name || "");
}
function selectorPrimaryLabel(kind, option) {
  if (option.type === "all") return option.name;
  if (option.type === "trace") return option.name;
  const pieces = [];
  if (phaseScopeFor(kind) === "all" && option.phase && option.phase !== "all") pieces.push(option.phase);
  const source = compactSource(option.file_name || option.name, option.phase);
  if (source) pieces.push(source);
  if (kind !== "TAS" && kind !== "STRAIN" && frequencyChannel !== "all") {
    const channel = channelOptionsForScope().find((item) => item.id === frequencyChannel);
    if (channel) pieces.push(channel.detail);
  }
  return pieces.join(" | ") || option.name;
}
function visibleOptions(kind) {
  const search = document.getElementById(`${kind}-search`);
  const query = search ? search.value.trim().toLowerCase() : "";
  const options = selectionOptionsFor(kind);
  if (!query) return options;
  return options.filter((option) => `${option.name} ${option.detail} ${option.file_name || ""} ${option.source_name || ""} ${option.phase || ""} ${option.mission || ""} ${option.channel || ""}`.toLowerCase().includes(query));
}
function renderFileList(kind) {
  const list = document.getElementById(`${kind}-list`);
  const options = visibleOptions(kind);
  const visible = getVisibleSet(kind);
  const rows = [];
  if (!options.length) {
    rows.push(`<div class="rank-row"><span class="rank-check-spacer"></span><button class="rank-button" type="button" disabled><span class="rank-number">0</span><span><span class="rank-label">No selections found</span></span></button><span class="rank-download-cell"></span></div>`);
  }
  options.forEach((option, index) => {
    const active = state[kind] === option.id ? "active" : "";
    const checked = visible.has("all") || visible.has(option.id) ? "checked" : "";
    const rowNumber = option.number || (option.type === "all" ? "All" : `#${index}`);
    const secondary = option.type === "all" ? option.detail : option.type === "trace" ? option.channel || option.detail : option.file_name || option.name;
    const meta = option.type === "all" ? "" : option.detail;
    const sourceName = option.source_name || option.file_name || option.name;
    const downloadControl = option.type === "all"
      ? ""
      : option.download_url
        ? `<a class="rank-download-button" href="${escapeHtml(option.download_url)}" target="_blank" rel="noopener" download title="Download original AWS file: ${escapeHtml(sourceName)}">Download</a>`
        : `<span class="rank-download-button disabled" title="Original file download is available after the dashboard uploads">Download</span>`;
    rows.push(`<div class="rank-row ${active}">
      <label class="rank-check"><input class="trace-toggle" type="checkbox" data-kind="${kind}" data-file="${escapeHtml(option.id)}" ${checked}></label>
      <button class="rank-button ${active}" type="button" data-kind="${kind}" data-file="${escapeHtml(option.id)}" title="${escapeHtml(option.file_name || option.name)}">
        <span class="rank-number">${escapeHtml(rowNumber)}</span>
        <span><span class="rank-label">${escapeHtml(selectorPrimaryLabel(kind, option))}</span>
        <span class="rank-full-name">${escapeHtml(secondary)}</span>
        ${meta ? `<span class="rank-value">${escapeHtml(meta)}</span>` : ""}</span>
      </button>
      <span class="rank-download-cell">${downloadControl}</span>
    </div>`);
  });
  list.innerHTML = rows.join("");
  list.querySelectorAll(".rank-button[data-file]").forEach((button) => {
    button.addEventListener("click", () => {
      state[kind] = button.dataset.file;
      setVisibleSet(kind, [button.dataset.file]);
      renderFileList(kind);
      renderChart(kind);
    });
  });
  list.querySelectorAll(".trace-toggle[data-file]").forEach((checkbox) => {
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => {
      const id = checkbox.dataset.file;
      if (id === "all") {
        setVisibleSet(kind, checkbox.checked ? ["all"] : []);
      } else {
        const selectableIds = selectionOptionsFor(kind).filter((item) => item.type !== "all").map((item) => item.id);
        const ids = getVisibleSet(kind).has("all") ? new Set(selectableIds) : new Set(getVisibleSet(kind));
        ids.delete("all");
        if (checkbox.checked) ids.add(id);
        else ids.delete(id);
        setVisibleSet(kind, ids.size === selectableIds.length ? ["all"] : ids);
      }
      renderFileList(kind);
      renderChart(kind);
    });
  });
  document.getElementById(`${kind}-show-all`).onclick = () => {
    state[kind] = "all";
    setVisibleSet(kind, ["all"]);
    renderFileList(kind);
    renderChart(kind);
  };
  document.getElementById(`${kind}-hide-all`).onclick = () => {
    setVisibleSet(kind, []);
    renderFileList(kind);
    renderChart(kind);
  };
  updateSectionCount(kind);
}
function updateSectionCount(kind) {
  const target = document.getElementById(`${kind}-count`);
  if (!target) return;
  const selectors = selectionOptionsFor(kind).filter((item) => item.type !== "all");
  const visible = getVisibleSet(kind);
  const shown = visible.has("all") ? selectors.length : visible.size;
  target.textContent = `${shown} shown / ${selectors.length} selections`;
}
function traceMatchesChannel(trace) {
  if (frequencyChannel === "all") return true;
  if (!frequencyChannel.startsWith("exact:")) return true;
  return `exact:${trace.channel}` === frequencyChannel;
}
function selectedRecords(kind) {
  const visible = getVisibleSet(kind);
  const records = recordsFor(kind);
  if (visible.size === 0) return [];
  if (visible.has("all")) return records;
  const ids = new Set(visible);
  return records.filter((record) => ids.has(record.id));
}
function tracesFor(kind) {
  const campaign = currentCampaign();
  const traces = [];
  const warnings = [];
  if (kind !== "TAS") {
    const visible = getVisibleSet(kind);
    if (visible.size === 0) return { traces, warnings };
    const selectedTraceIds = visible.has("all") ? null : new Set(visible);
    for (const record of recordsFor(kind)) {
      const parsed = campaign.series[record.id];
      if (!parsed) continue;
      (parsed.traces || []).forEach((trace, traceIndex) => {
        if (!traceAllowedForKind(kind, trace)) return;
        if (selectedTraceIds && !selectedTraceIds.has(traceOptionId(record, traceIndex))) return;
        if (kind === "STRAIN") {
          const scaleSquared = strainScale * strainScale;
          traces.push({ ...trace, y: trace.y.map((value) => value * scaleSquared), y_label: "PSD (microstrain^2/Hz)" });
        } else {
          traces.push(trace);
        }
      });
      for (const warning of parsed.warnings || []) warnings.push(`${record.name}: ${warning}`);
    }
    return { traces, warnings };
  }
  for (const record of selectedRecords(kind)) {
    const parsed = campaign.series[record.id];
    if (!parsed) continue;
    for (const trace of parsed.traces || []) {
      traces.push(trace);
    }
    for (const warning of parsed.warnings || []) warnings.push(`${record.name}: ${warning}`);
  }
  return { traces, warnings };
}
function traceStyle(index, selected) {
  if (selected) return { color: "#000000", width: 2.4 };
  return { color: palette[index % palette.length], width: 1.2 };
}
function compactLegendName(name) {
  const parts = String(name).split(" - ");
  let source = parts.shift() || "";
  const channel = parts.join(" - ");
  source = source.replace(/^.*?_TSfilt_/, "").replace(/_(ERS|FDS|PSD|PSD_STRAIN)$/i, "").replace(/TAS$/i, "");
  const detail = source.replace(/_/g, " ").trim();
  return [detail, channel].filter(Boolean).join(" | ") || name;
}
function plotlyTraces(kind, traces) {
  const selected = traces.length === 1;
  return traces.map((trace, index) => ({
    x: trace.x,
    y: trace.y,
    type: "scatter",
    mode: "lines",
    name: compactLegendName(trace.name),
    line: traceStyle(index, selected),
    opacity: selected ? 1 : 0.72,
    hovertemplate: `${escapeHtml(trace.name)}<br>%{x:.6g}<br>%{y:.6g}<extra></extra>`,
    showlegend: selected || traces.length <= 24,
  }));
}
function layoutFor(kind, traces) {
  const group = currentCampaign().groups[kind];
  const hasData = traces.length > 0;
  const isFrequency = kind !== "TAS";
  const useSideLegend = traces.length > 1 && window.innerWidth >= 1180;
  const xTitle = traces[0]?.x_label || group.x_title;
  const yTitle = kind === "STRAIN" ? "PSD (microstrain^2/Hz)" : (traces[0]?.y_label || group.y_title);
  const titleParts = [group.title, activePhase === "all" ? "All Phases" : activePhase];
  if (missionFilter !== "all") titleParts.push(missionFilter);
  const xaxis = {
    type: isFrequency ? "log" : "linear",
    title: { text: xTitle, font: { size: 11, color: "#000000" } },
    tickfont: { size: 10, color: "#000000" },
    color: "#000000",
    showline: true,
    linecolor: "#000000",
    mirror: true,
    gridcolor: "#d0d0d0",
    zeroline: false,
    ticks: "outside",
    ticklen: 5,
    tickwidth: 1,
  };
  if (isFrequency) Object.assign(xaxis, { range: [1, 3], tickmode: "array", tickvals: frequencyTickVals, ticktext: frequencyTickText, showgrid: true });
  const layout = {
    title: { text: titleParts.join(" | "), font: { size: 14, color: "#000000" } },
    autosize: true,
    paper_bgcolor: "#ffffff",
    plot_bgcolor: "#ffffff",
    font: { family: "Arial, Helvetica, sans-serif", color: "#000000", size: 11 },
    margin: { l: 68, r: useSideLegend ? 190 : 18, t: 42, b: useSideLegend ? 52 : 118 },
    legend: useSideLegend ? {
      x: 1.01, y: 1, xanchor: "left", yanchor: "top", bgcolor: "rgba(255,255,255,0.96)",
      bordercolor: "#000000", borderwidth: 1, font: { size: 9, color: "#000000" }, itemsizing: "constant",
    } : {
      x: 0, y: -0.24, xanchor: "left", yanchor: "top", orientation: "h", bgcolor: "rgba(255,255,255,0.96)",
      bordercolor: "#000000", borderwidth: 1, font: { size: 9, color: "#000000" }, itemsizing: "constant",
    },
    xaxis,
    yaxis: {
      type: isFrequency ? "log" : "linear",
      title: { text: yTitle, font: { size: 11, color: "#000000" } },
      tickfont: { size: 10, color: "#000000" },
      color: "#000000",
      showline: true,
      linecolor: "#000000",
      mirror: true,
      gridcolor: "#d0d0d0",
      zeroline: false,
    },
  };
  if (!hasData) {
    layout.annotations = [{ text: `No ${kind} data to plot`, xref: "paper", yref: "paper", x: 0.5, y: 0.5, showarrow: false, font: { size: 13, color: "#475569" } }];
  }
  return layout;
}
function renderWarnings(kind, warnings) {
  const target = document.getElementById(`${kind}-warnings`);
  if (!warnings.length) {
    target.classList.remove("visible");
    target.classList.remove("expanded");
    target.innerHTML = "";
    return;
  }
  const shownWarnings = warnings.slice(0, 8);
  const hiddenCount = Math.max(0, warnings.length - shownWarnings.length);
  const isExpanded = !!warningExpanded[kind];
  target.classList.add("visible");
  target.classList.toggle("expanded", isExpanded);
  target.innerHTML = `<button class="warning-toggle" type="button" id="${kind}-warning-toggle" aria-expanded="${isExpanded ? "true" : "false"}">
      <span class="warning-summary">${escapeHtml(warnings.length === 1 ? "1 warning" : `${warnings.length} warnings`)}${hiddenCount ? escapeHtml(` (${shownWarnings.length} shown)`) : ""}</span>
      <span class="warning-action">${isExpanded ? "Hide details" : "Show details"}</span>
    </button>
    <div class="warning-body">
      ${shownWarnings.map((warning) => `<div class="warning-item">${escapeHtml(warning)}</div>`).join("")}
      ${hiddenCount ? `<div class="warning-item">${escapeHtml(`${hiddenCount} more warnings hidden. Narrow the selection to see fewer messages.`)}</div>` : ""}
    </div>`;
  document.getElementById(`${kind}-warning-toggle`).addEventListener("click", () => {
    warningExpanded[kind] = !warningExpanded[kind];
    renderWarnings(kind, warnings);
  });
}
function renderChart(kind) {
  const plot = document.getElementById(`${kind}-plot`);
  if (!plot) return;
  const { traces, warnings } = tracesFor(kind);
  Plotly.react(plot, plotlyTraces(kind, traces), layoutFor(kind, traces), { responsive: true, displayModeBar: false });
  renderWarnings(kind, warnings);
  updateSectionCount(kind);
}
function buildSections() {
  elements.sections.innerHTML = KINDS.map((kind) => sectionHtml(kind)).join("");
  for (const kind of KINDS) {
    renderFileList(kind);
    document.getElementById(`${kind}-search`).addEventListener("input", () => renderFileList(kind));
  }
}
function updateStatus() {
  const counts = KINDS.map((kind) => `${kind}: ${selectionOptionsFor(kind).filter((item) => item.type !== "all").length}`).join(" | ");
  elements.status.textContent = `Ready. Campaign ${campaignId} | Mission ${missionFilter === "all" ? "All" : missionFilter} | TAS phase ${activePhase === "all" ? "All" : activePhase} | Frequency and strain follow TAS phase | ${counts}`;
  elements.currentView.textContent = `Zip ${campaignId} | Mission ${missionFilter === "all" ? "All" : missionFilter} | Phase ${activePhase === "all" ? "All" : activePhase}`;
}
function renderAllCharts() {
  for (const kind of KINDS) renderChart(kind);
}
function rebuildAll() {
  buildCampaignFilter();
  buildMissionFilter();
  buildPhaseFilter();
  buildFrequencyChannelFilter();
  buildSections();
  renderAllCharts();
  updateStatus();
}
function initializeDefaults() {
  const campaign = currentCampaign();
  missionFilter = campaign?.missions?.[0]?.id || "all";
  activePhase = "all";
  frequencyChannel = "all";
  strainScale = Number(elements.strainScale.value) || 1;
  elements.s3.textContent = payload.source_s3_uri;
  elements.generated.textContent = payload.generated;
  elements.strainScale.addEventListener("input", () => {
    const value = Number(elements.strainScale.value);
    strainScale = Number.isFinite(value) ? value : 1;
    renderChart("STRAIN");
  });
  rebuildAll();
}
initializeDefaults();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
