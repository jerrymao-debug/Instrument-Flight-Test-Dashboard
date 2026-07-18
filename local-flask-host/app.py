from __future__ import annotations

import hmac
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from flask import Flask, Response, abort, jsonify, request, send_file


BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = Path(os.getenv("DASHBOARD_CACHE_DIR", BASE_DIR / "cache")).resolve()
BUCKET = os.getenv("DASHBOARD_S3_BUCKET", "vibration-data-daq").strip()
PREFIX = os.getenv(
    "DASHBOARD_S3_PREFIX", "insturment_fly_test_dashboard_code/"
).strip("/") + "/"
AWS_REGION = os.getenv("AWS_REGION", "us-west-2").strip()
AWS_PROFILE = os.getenv("AWS_PROFILE", "ncode-sso").strip()
PUBLIC_BASE_URL = os.getenv(
    "DASHBOARD_PUBLIC_BASE_URL",
    f"https://{BUCKET}.s3.{AWS_REGION}.amazonaws.com/{PREFIX.rstrip('/')}",
).rstrip("/")
REFRESH_SECONDS = max(0, int(os.getenv("DASHBOARD_REFRESH_SECONDS", "600")))
REFRESH_TOKEN = os.getenv("DASHBOARD_REFRESH_TOKEN", "")
INDEX_FILE = "index.html"

app = Flask(__name__)
_refresh_lock = threading.Lock()
_state_lock = threading.Lock()
_state: dict[str, Any] = {
    "last_attempt": None,
    "last_success": None,
    "last_error": None,
    "source": None,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_relative_path(value: str) -> str:
    value = urllib.parse.unquote(value).replace("\\", "/").lstrip("/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError("Invalid dashboard path")
    return path.as_posix()


def _cache_path(relative_path: str) -> Path:
    safe_path = _safe_relative_path(relative_path)
    candidate = (CACHE_DIR / Path(*PurePosixPath(safe_path).parts)).resolve()
    if CACHE_DIR != candidate and CACHE_DIR not in candidate.parents:
        raise ValueError("Path is outside the cache directory")
    return candidate


def _atomic_write(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, target)


def _download_with_boto3(relative_path: str) -> bytes:
    import boto3
    from botocore.exceptions import ProfileNotFound

    try:
        session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    except ProfileNotFound:
        session = boto3.Session(region_name=AWS_REGION)
    response = session.client("s3").get_object(
        Bucket=BUCKET,
        Key=f"{PREFIX}{relative_path}",
    )
    return response["Body"].read()


def _download_with_https(relative_path: str) -> bytes:
    encoded_path = urllib.parse.quote(relative_path, safe="/")
    url = f"{PUBLIC_BASE_URL}/{encoded_path}"
    request_object = urllib.request.Request(
        url,
        headers={"User-Agent": "Instrument-Flight-Test-Dashboard-Local/1.0"},
    )
    with urllib.request.urlopen(request_object, timeout=30) as response:
        return response.read()


def download_to_cache(relative_path: str) -> tuple[Path, str]:
    relative_path = _safe_relative_path(relative_path)
    target = _cache_path(relative_path)
    errors: list[str] = []

    try:
        payload = _download_with_boto3(relative_path)
        source = "authenticated S3"
    except Exception as error:  # HTTPS is the deliberate fallback.
        errors.append(f"S3: {error}")
        try:
            payload = _download_with_https(relative_path)
            source = "public S3 HTTPS"
        except Exception as public_error:
            errors.append(f"HTTPS: {public_error}")
            raise RuntimeError("; ".join(errors)) from public_error

    _atomic_write(target, payload)
    return target, source


def refresh_dashboard() -> dict[str, Any]:
    with _refresh_lock:
        with _state_lock:
            _state["last_attempt"] = _utc_now()
        try:
            target, source = download_to_cache(INDEX_FILE)
            with _state_lock:
                _state.update(
                    last_success=_utc_now(),
                    last_error=None,
                    source=source,
                )
            return {"ok": True, "path": str(target), "source": source}
        except Exception as error:
            with _state_lock:
                _state["last_error"] = str(error)
            return {"ok": False, "error": str(error)}


def _status() -> dict[str, Any]:
    index_path = _cache_path(INDEX_FILE)
    with _state_lock:
        snapshot = dict(_state)
    snapshot.update(
        {
            "bucket": BUCKET,
            "prefix": PREFIX,
            "cached": index_path.exists(),
            "cache_path": str(index_path),
            "refresh_seconds": REFRESH_SECONDS,
        }
    )
    if index_path.exists():
        snapshot["cached_file_modified"] = datetime.fromtimestamp(
            index_path.stat().st_mtime, timezone.utc
        ).isoformat()
    return snapshot


def _refresh_is_allowed() -> bool:
    supplied = request.headers.get("X-Refresh-Token", "")
    if REFRESH_TOKEN:
        return hmac.compare_digest(supplied, REFRESH_TOKEN)
    return request.remote_addr in {"127.0.0.1", "::1"}


@app.get("/")
def dashboard() -> Response:
    index_path = _cache_path(INDEX_FILE)
    if not index_path.exists():
        result = refresh_dashboard()
        if not result["ok"]:
            return Response(
                "Dashboard is not available yet. Open /api/status for details.",
                status=503,
                mimetype="text/plain",
            )
    response = send_file(index_path, mimetype="text/html", conditional=True)
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/status")
def api_status() -> Response:
    return jsonify(_status())


@app.post("/api/refresh")
def api_refresh() -> tuple[Response, int] | Response:
    if not _refresh_is_allowed():
        abort(403)
    result = refresh_dashboard()
    return jsonify(result), (200 if result["ok"] else 502)


@app.get("/<path:asset_path>")
def dashboard_asset(asset_path: str) -> Response:
    try:
        relative_path = _safe_relative_path(asset_path)
        target = _cache_path(relative_path)
    except ValueError:
        abort(404)

    if not target.exists():
        try:
            target, _ = download_to_cache(relative_path)
        except (RuntimeError, urllib.error.URLError):
            abort(404)

    content_type, _ = mimetypes.guess_type(target.name)
    return send_file(target, mimetype=content_type, conditional=True)


def _background_refresh() -> None:
    while True:
        refresh_dashboard()
        if REFRESH_SECONDS == 0:
            return
        time.sleep(REFRESH_SECONDS)


def main() -> None:
    from waitress import serve

    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "5000"))
    threading.Thread(target=_background_refresh, daemon=True).start()
    print(f"Dashboard server: http://{host}:{port}", flush=True)
    print(f"AWS source: s3://{BUCKET}/{PREFIX}", flush=True)
    serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
