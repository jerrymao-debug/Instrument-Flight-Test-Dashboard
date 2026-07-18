# Local Instrument Flight Test Dashboard

This small Flask application hosts the published Instrument Flight Test Dashboard on this Windows computer.

## What it does

1. The Python server downloads `index.html` from:
   `s3://vibration-data-daq/insturment_fly_test_dashboard_code/`
2. It saves a local cached copy.
3. Flask/Waitress sends that copy to the browser at `http://127.0.0.1:5000/`.
4. It refreshes the copy from AWS every 10 minutes. If AWS is temporarily unavailable, the last good copy remains available.

AWS access stays on the server. The browser never receives AWS credentials.

## Start it

Right-click `start-dashboard.ps1` and choose **Run with PowerShell**, or run:

```powershell
PowerShell -ExecutionPolicy Bypass -File .\start-dashboard.ps1
```

The script installs the required Python packages into the local `.runtime` folder the first time, starts the server, and opens the dashboard.

Stop the server with `Ctrl+C` in its PowerShell window.

## Install on another Windows computer

Install Python 3.10 or newer, then clone the hosting branch and start the server:

```powershell
git clone --branch flask-local-host-07.18.2026 --single-branch https://github.com/jerrymao-debug/Instrument-Flight-Test-Dashboard.git
cd .\Instrument-Flight-Test-Dashboard\local-flask-host
PowerShell -ExecutionPolicy Bypass -File .\start-dashboard.ps1
```

If Git is not installed, download the branch as a ZIP from GitHub, extract it, open the `local-flask-host` folder, and run `start-dashboard.ps1`.

## Useful endpoints

- Dashboard: `http://127.0.0.1:5000/`
- Server status: `http://127.0.0.1:5000/api/status`
- Force a refresh from this computer:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:5000/api/refresh
```

## Authenticated/private S3 access

The server first tries the AWS profile in `AWS_PROFILE` (default: `ncode-sso`). If that profile is unavailable, it uses the current public S3 HTTPS endpoint. For private S3 access, install/configure the AWS CLI and sign in before starting:

```powershell
$env:AWS_PROFILE = "ncode-sso"
aws sso login --profile ncode-sso
.\start-dashboard.ps1
```

## Share over Tailscale

The safe default is local-only. To let another computer on your Tailscale network connect, start it with:

```powershell
$env:DASHBOARD_HOST = "0.0.0.0"
$env:DASHBOARD_REFRESH_TOKEN = "choose-a-long-random-secret"
.\start-dashboard.ps1
```

Then browse to `http://<this-computer-Tailscale-IP>:5000/`. Windows Firewall may ask you to allow Python on private networks. Do not expose port 5000 directly to the public internet.

## Configuration

Environment variables can override the defaults:

- `DASHBOARD_S3_BUCKET`
- `DASHBOARD_S3_PREFIX`
- `DASHBOARD_PUBLIC_BASE_URL`
- `DASHBOARD_REFRESH_SECONDS`
- `DASHBOARD_HOST`
- `DASHBOARD_PORT`
- `DASHBOARD_REFRESH_TOKEN`
- `AWS_PROFILE`
- `AWS_REGION`

Source project: <https://github.com/jerrymao-debug/Instrument-Flight-Test-Dashboard/tree/Instrument-Flight-Test-Dashboard-07.18.2026>
