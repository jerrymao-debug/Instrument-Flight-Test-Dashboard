from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse


SOURCE_DIR = Path(r"C:\Users\jerry\Desktop\new FDS\Processing data\4_psd")
DESTINATION_S3_URI = "s3://vibration-data-daq/Instrumented fly test dashboard/808/"
LOG_DIR = Path(__file__).resolve().parent / "logs"
PREFERRED_AWS_PROFILE = "ncode-sso"

SKIP_DIR_PREFIXES = ("_",)
TAS_SUFFIX = "TAS.csv"
RESULT_EXTENSIONS = {".xmh"}


@dataclass
class UploadStats:
    scanned: int = 0
    uploaded: int = 0
    skipped_same: int = 0
    failed: int = 0


class AwsLoginNeeded(RuntimeError):
    pass


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


def s3_key_for_file(path: Path, root: Path, prefix: str) -> str:
    relative = path.relative_to(root)
    return f"{prefix}{PurePosixPath(*relative.parts).as_posix()}"


def find_duplicate_s3_keys(files: list[Path], root: Path, prefix: str) -> dict[str, list[Path]]:
    by_key: dict[str, list[Path]] = {}
    for path in files:
        by_key.setdefault(s3_key_for_file(path, root, prefix), []).append(path)
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
    writer.writerow(["status", "local_path", "s3_uri", "local_size", "remote_size", "message"])
    return manifest_path, handle, writer


def upload_outputs(args: argparse.Namespace) -> int:
    root = Path(args.source).resolve()
    bucket, prefix = split_s3_uri(args.destination)
    profile = choose_aws_profile(args.profile)

    if not root.exists():
        print(f"Source folder does not exist: {root}")
        return 1

    files = iter_final_outputs(root)
    print(f"Source: {root}")
    print(f"Destination: s3://{bucket}/{prefix}")
    print(f"AWS profile: {profile or 'default credential chain'}")
    print(f"Final output files found: {len(files)}")

    if not files:
        print("No final output files found. Expected .xmh files and TAS.csv files.")
        return 1

    duplicate_keys = find_duplicate_s3_keys(files, root, prefix)
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
            key = s3_key_for_file(path, root, prefix)
            s3_uri = f"s3://{bucket}/{key}"

            try:
                before_size = None if args.force else remote_size(s3_client, bucket, key)
                if before_size == local_size and not args.force:
                    stats.skipped_same += 1
                    manifest_writer.writerow(["skipped_same", str(path), s3_uri, local_size, before_size, "already uploaded"])
                    print(f"[{index}/{len(files)}] skip same: {path.name}")
                    continue

                if args.dry_run:
                    manifest_writer.writerow(["dry_run", str(path), s3_uri, local_size, before_size, "would upload"])
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
                manifest_writer.writerow(["uploaded", str(path), s3_uri, local_size, after_size, message])
                print(f"[{index}/{len(files)}] uploaded: {path.name}")
            except Exception as exc:
                if is_login_error(exc):
                    raise AwsLoginNeeded(str(exc)) from exc
                stats.failed += 1
                manifest_writer.writerow(["failed", str(path), s3_uri, local_size, "", str(exc)])
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
    parser.add_argument("--destination", default=DESTINATION_S3_URI, help="Destination S3 URI.")
    parser.add_argument("--profile", default=None, help="AWS profile name. Defaults to AWS_PROFILE, then ncode-sso.")
    parser.add_argument("--force", action="store_true", help="Upload every final output even if the S3 object already exists.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be uploaded without sending files.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return upload_outputs(args)


if __name__ == "__main__":
    raise SystemExit(main())
