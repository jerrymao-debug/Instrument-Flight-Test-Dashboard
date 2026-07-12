from __future__ import annotations

import argparse
from collections import Counter
import csv
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse


SOURCE_DIR = Path(r"C:\Users\jerry\Desktop\new FDS\Processing data\4_psd")
DESTINATION_S3_URI = "s3://vibration-data-daq/Instrumented fly test dashboard/"
MISSION_AIRCRAFT_MAP = Path(__file__).resolve().parent / "mission_aircraft_map.csv"
LOG_DIR = Path(__file__).resolve().parent / "logs"
PREFERRED_AWS_PROFILE = "ncode-sso"

SKIP_DIR_PREFIXES = ("_",)
TAS_SUFFIX = "TAS.csv"
RESULT_EXTENSIONS = {".xmh"}
MISSION_ID_PATTERN = re.compile(r"(P2M_[A-Za-z0-9]+)")


@dataclass
class UploadStats:
    scanned: int = 0
    uploaded: int = 0
    skipped_same: int = 0
    failed: int = 0


class AwsLoginNeeded(RuntimeError):
    pass


def normalize_mission_id(value: str) -> str:
    return value.strip().upper()


def mission_id_from_name(name: str) -> str | None:
    match = MISSION_ID_PATTERN.search(name)
    if not match:
        return None
    return normalize_mission_id(match.group(1))


def load_mission_aircraft_map(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"Mission aircraft map does not exist: {path}")

    mission_to_aircraft: dict[str, str] = {}
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"mission_id", "aircraft_id"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Mission map is missing column(s): {', '.join(sorted(missing))}")

        for row_number, row in enumerate(reader, start=2):
            mission = normalize_mission_id(row.get("mission_id", ""))
            aircraft = (row.get("aircraft_id") or "").strip()
            if not mission or not aircraft:
                continue
            if mission in mission_to_aircraft and mission_to_aircraft[mission] != aircraft:
                raise ValueError(
                    f"Mission {mission} maps to both {mission_to_aircraft[mission]} and {aircraft}; "
                    f"check {path} row {row_number}."
                )
            mission_to_aircraft[mission] = aircraft

    if not mission_to_aircraft:
        raise ValueError(f"Mission map did not contain any mission_id/aircraft_id rows: {path}")
    return mission_to_aircraft


def split_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Not a valid S3 URI: {uri}")

    prefix = parsed.path.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return parsed.netloc, prefix


def choose_aws_profile(requested_profile: str | None) -> str | None:
    env_profile = os.environ.get("AWS_PROFILE")
    if requested_profile:
        return requested_profile
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
        print("Missing Python package: boto3")
        print("Install it with:")
        print("  python -m pip install boto3")
        raise SystemExit(1)

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client("s3")


def is_login_error(exc: Exception) -> bool:
    class_name = exc.__class__.__name__
    if class_name in {
        "NoCredentialsError",
        "PartialCredentialsError",
        "SSOTokenLoadError",
        "UnauthorizedSSOTokenError",
        "TokenRetrievalError",
    }:
        return True

    message = str(exc).lower()
    return any(
        text in message
        for text in (
            "unable to locate credentials",
            "sso session",
            "token has expired",
            "unauthorized",
            "could not automatically refresh",
        )
    )


def print_login_help(profile: str | None) -> None:
    profile_text = profile or PREFERRED_AWS_PROFILE
    print()
    print("AWS login is not ready.")
    print("Run this first, then run the upload again:")
    print(f"  aws sso login --profile {profile_text}")


def remote_size(s3_client, bucket: str, key: str) -> int | None:
    try:
        response = s3_client.head_object(Bucket=bucket, Key=key)
        return int(response.get("ContentLength", 0))
    except Exception as exc:
        if is_login_error(exc):
            raise AwsLoginNeeded(str(exc)) from exc

        error = getattr(exc, "response", {}).get("Error", {})
        code = str(error.get("Code", ""))
        status = str(getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
        if code in {"404", "NoSuchKey", "NotFound"} or status == "404":
            return None
        raise


def is_final_output(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part.startswith(SKIP_DIR_PREFIXES) for part in relative.parts[:-1]):
        return False

    name = path.name
    suffix = path.suffix.lower()
    return suffix in RESULT_EXTENSIONS or name.endswith(TAS_SUFFIX)


def iter_final_outputs(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file() and is_final_output(path, root))


def route_for_file(path: Path, mission_to_aircraft: dict[str, str]) -> tuple[str, str]:
    mission = mission_id_from_name(path.name)
    if not mission:
        raise ValueError(f"Could not find mission ID in file name: {path.name}")
    aircraft = mission_to_aircraft.get(mission)
    if not aircraft:
        raise KeyError(mission)
    return mission, aircraft


def s3_key_for_file(path: Path, root: Path, prefix: str, mission_to_aircraft: dict[str, str]) -> str:
    _, aircraft = route_for_file(path, mission_to_aircraft)
    relative = path.relative_to(root)
    return f"{prefix}{aircraft}/{PurePosixPath(*relative.parts).as_posix()}"


def find_duplicate_s3_keys(files: list[Path], root: Path, prefix: str, mission_to_aircraft: dict[str, str]) -> dict[str, list[Path]]:
    by_key: dict[str, list[Path]] = {}
    for path in files:
        by_key.setdefault(s3_key_for_file(path, root, prefix, mission_to_aircraft), []).append(path)
    return {key: paths for key, paths in by_key.items() if len(paths) > 1}


def find_duplicate_file_names(files: list[Path]) -> dict[str, list[Path]]:
    by_name: dict[str, list[Path]] = {}
    for path in files:
        by_name.setdefault(path.name.lower(), []).append(path)
    return {name: paths for name, paths in by_name.items() if len(paths) > 1}


def open_manifest() -> tuple[Path, object, csv.writer]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_path = LOG_DIR / f"upload_to_aws_{stamp}.csv"
    handle = manifest_path.open("w", newline="", encoding="utf-8")
    writer = csv.writer(handle)
    writer.writerow(["status", "aircraft_id", "mission_id", "local_path", "s3_uri", "local_size", "remote_size", "message"])
    return manifest_path, handle, writer


def upload_outputs(args: argparse.Namespace) -> int:
    root = Path(args.source).resolve()
    bucket, prefix = split_s3_uri(args.destination)
    map_path = Path(args.mission_map).resolve()
    profile = choose_aws_profile(args.profile)

    if not root.exists():
        print(f"Source folder does not exist: {root}")
        return 1

    files = iter_final_outputs(root)
    print(f"Source: {root}")
    print(f"Destination: s3://{bucket}/{prefix}")
    print(f"Mission map: {map_path}")
    print(f"AWS profile: {profile or 'default credential chain'}")
    print(f"Final output files found: {len(files)}")

    if not files:
        print("No final output files found. Expected .xmh files and TAS.csv files.")
        return 1

    try:
        mission_to_aircraft = load_mission_aircraft_map(map_path)
    except Exception as exc:
        print(f"Could not load mission aircraft map: {exc}")
        return 1

    file_routes: dict[Path, tuple[str, str]] = {}
    missing_missions: dict[str, list[Path]] = {}
    for path in files:
        mission = mission_id_from_name(path.name)
        if not mission:
            missing_missions.setdefault("<no mission id>", []).append(path)
            continue
        aircraft = mission_to_aircraft.get(mission)
        if not aircraft:
            missing_missions.setdefault(mission, []).append(path)
            continue
        file_routes[path] = (mission, aircraft)

    if missing_missions:
        print("Upload stopped because some mission IDs are not in the mission aircraft map.")
        for mission, paths in sorted(missing_missions.items()):
            print(f"  {mission}: {len(paths)} file(s)")
            for path in paths[:5]:
                print(f"    {path}")
            if len(paths) > 5:
                print(f"    ... {len(paths) - 5} more")
        print()
        print(f"Add these mission IDs to: {map_path}")
        return 1

    route_counts = Counter(aircraft for _, aircraft in file_routes.values())
    print("Aircraft routing:")
    for aircraft, count in sorted(route_counts.items(), key=lambda item: item[0]):
        print(f"  {aircraft}: {count} file(s)")

    duplicate_keys = find_duplicate_s3_keys(files, root, prefix, mission_to_aircraft)
    if duplicate_keys:
        print("Duplicate S3 destination keys found. Upload stopped so no file overwrites another one.")
        for key, paths in duplicate_keys.items():
            print(f"  s3://{bucket}/{key}")
            for path in paths:
                print(f"    {path}")
        return 1

    duplicate_names = find_duplicate_file_names(files)
    if duplicate_names:
        print(f"Warning: {len(duplicate_names)} duplicate file names exist in different folders.")
        print("They are safe because their phase folders make the S3 keys different.")
    else:
        print("Duplicate check: no duplicate file names or S3 keys found.")

    manifest_path, manifest_handle, manifest_writer = open_manifest()
    stats = UploadStats(scanned=len(files))

    try:
        s3_client = get_s3_client(profile)

        for index, path in enumerate(files, start=1):
            local_size = path.stat().st_size
            mission_id, aircraft_id = file_routes[path]
            key = s3_key_for_file(path, root, prefix, mission_to_aircraft)
            s3_uri = f"s3://{bucket}/{key}"

            try:
                before_size = None if args.force else remote_size(s3_client, bucket, key)
                if before_size == local_size and not args.force:
                    stats.skipped_same += 1
                    manifest_writer.writerow(["skipped_same", aircraft_id, mission_id, str(path), s3_uri, local_size, before_size, "already uploaded"])
                    print(f"[{index}/{len(files)}] skip same: {path.name}")
                    continue

                if args.dry_run:
                    manifest_writer.writerow(["dry_run", aircraft_id, mission_id, str(path), s3_uri, local_size, before_size, "would upload"])
                    print(f"[{index}/{len(files)}] dry run: {path.name}")
                    continue

                s3_client.upload_file(str(path), bucket, key)
                after_size = remote_size(s3_client, bucket, key)
                if after_size != local_size:
                    raise RuntimeError(f"upload verification failed: local={local_size}, remote={after_size}")

                stats.uploaded += 1
                message = "uploaded"
                if before_size is not None and before_size != local_size:
                    message = f"uploaded; replaced remote size {before_size}"
                manifest_writer.writerow(["uploaded", aircraft_id, mission_id, str(path), s3_uri, local_size, after_size, message])
                print(f"[{index}/{len(files)}] uploaded to {aircraft_id}: {path.name}")
            except Exception as exc:
                if is_login_error(exc):
                    raise AwsLoginNeeded(str(exc)) from exc
                stats.failed += 1
                manifest_writer.writerow(["failed", aircraft_id, mission_id, str(path), s3_uri, local_size, "", str(exc)])
                print(f"[{index}/{len(files)}] failed: {path.name}: {exc}")

    except AwsLoginNeeded as exc:
        print_login_help(profile)
        print(f"Details: {exc}")
        return 2
    finally:
        manifest_handle.close()

    print()
    print("Upload summary")
    print(f"  scanned: {stats.scanned}")
    print(f"  uploaded: {stats.uploaded}")
    print(f"  skipped same: {stats.skipped_same}")
    print(f"  failed: {stats.failed}")
    print(f"  manifest: {manifest_path}")

    return 0 if stats.failed == 0 else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload final new FDS outputs to AWS S3.")
    parser.add_argument("--source", default=str(SOURCE_DIR), help="Folder containing the finished phase output folders.")
    parser.add_argument("--destination", default=DESTINATION_S3_URI, help="Base destination S3 URI. Aircraft folders are added automatically.")
    parser.add_argument("--mission-map", default=str(MISSION_AIRCRAFT_MAP), help="CSV with mission_id and aircraft_id columns.")
    parser.add_argument("--profile", default=None, help="AWS profile name. Defaults to AWS_PROFILE, then ncode-sso.")
    parser.add_argument("--force", action="store_true", help="Upload every final output even if the S3 object already exists.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be uploaded without sending files.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return upload_outputs(args)


if __name__ == "__main__":
    raise SystemExit(main())
