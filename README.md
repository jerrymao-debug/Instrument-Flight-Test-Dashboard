# Instrument Flight Test Dashboard

This repository contains the source code for the Instrument Flight Test dashboard and the nCode processing/upload helpers.

Current temporary public dashboard:

https://vibration-data-daq.s3.us-west-2.amazonaws.com/insturment_fly_test_dashboard_code/index.html

For durable hosting, use CloudFront in front of the private S3 prefix. See:

```text
docs/cloudfront_private_s3.md
```

## Local Flask Hosting

The `local-flask-host/` folder contains a portable Flask/Waitress server that downloads the published dashboard from AWS, keeps a local cached copy, and serves it from a Windows computer.

Detailed instructions: [`local-flask-host/README.md`](local-flask-host/README.md)

Start it from the repository root:

```powershell
cd .\local-flask-host
PowerShell -ExecutionPolicy Bypass -File .\start-dashboard.ps1
```

Open the dashboard on the hosting computer:

```text
http://127.0.0.1:5000/
```

To share it with other computers on the same Tailscale network:

```powershell
$env:DASHBOARD_HOST = "0.0.0.0"
.\start-dashboard.ps1
```

The dashboard currently hosted on Jerry's computer is available to connected Tailscale devices at:

```text
http://100.92.170.94:5000/
```

This Tailscale address is private, not a public internet URL. The hosting computer must remain powered on, connected to Tailscale, and running the Flask server.

## Repository Layout

- `dashboard/dashboard_builder.py` builds and uploads the static interactive dashboard from S3 source data.
- `dashboard/databricks_dashboard_refresh.py` is the Databricks loop that refreshes the dashboard every 10 minutes.
- `dashboard/deploy_public_dashboard.py` documents/deploys the public S3/CloudFront-style hosting path when permissions allow it.
- `dashboard/deploy_password_protected_dashboard.py` is an optional shared-password CloudFront deployment helper.
- `local-flask-host/` contains the portable Windows Flask/Waitress host and its setup guide.
- `pipeline/` contains local nCode processing helpers for CSV translation, phase splitting, FDS/ERS/PSD/strain/TAS generation, and S3 upload.

## Dashboard Data Flow

Source data:

```text
s3://vibration-data-daq/Instrumented fly test dashboard/
```

Published dashboard/code:

```text
s3://vibration-data-daq/insturment_fly_test_dashboard_code/
```

The dashboard builder:

1. scans source `.xmh` and `TAS.csv` files,
2. parses TAS, ERS, FDS, PSD, and strain data,
3. builds a static `index.html`,
4. mirrors original source files into the public dashboard prefix for permanent row-level downloads,
5. uploads the dashboard and manifest to S3.

## Local Setup

```powershell
python -m pip install -r requirements.txt
aws sso login --profile ncode-sso
```

## Build And Upload Dashboard

From the repo root:

```powershell
python .\dashboard\dashboard_builder.py --force --profile ncode-sso
```

Normal refresh, only uploading when source data changed:

```powershell
python .\dashboard\dashboard_builder.py --profile ncode-sso
```

The builder defaults to stable public links under:

```text
https://vibration-data-daq.s3.us-west-2.amazonaws.com/insturment_fly_test_dashboard_code
```

To override that:

```powershell
python .\dashboard\dashboard_builder.py --public-base-url "https://example.com/dashboard" --profile ncode-sso
```

## Databricks Refresh

Paste or import:

```text
dashboard/databricks_dashboard_refresh.py
```

That script downloads `dashboard_builder.py` from S3 and runs it every 600 seconds.

To switch Databricks output links to CloudFront after infra creates the distribution, set:

```python
import os
os.environ["DASHBOARD_PUBLIC_BASE_URL"] = "https://<cloudfront-domain>"
```

The same value can be passed locally with `--public-base-url`.

## nCode Pipeline

Main local pipeline:

```powershell
cd .\pipeline
python .\final_code.py
```

Useful options:

```powershell
python .\final_code.py --limit 1
python .\final_code.py --skip-translate
python .\final_code.py --skip-translate --skip-split
python .\final_code.py --overwrite
```

Upload processed results to S3:

```powershell
python .\upload_to_aws.py --profile ncode-sso
```

## Notes

- Do not commit generated dashboards, caches, logs, presigned URLs, or credential files.
- The current raw S3 public access is scoped to `insturment_fly_test_dashboard_code/*`, but it can be reset by bucket security automation.
- The durable hosting path is CloudFront with private S3; see `docs/cloudfront_private_s3.md`.
- The optional password-protected CloudFront deployment requires additional CloudFront IAM permissions.
