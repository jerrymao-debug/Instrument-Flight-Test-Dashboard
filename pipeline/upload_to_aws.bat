@echo off
setlocal
cd /d "%~dp0"

aws sts get-caller-identity --profile ncode-sso >nul 2>nul
if errorlevel 1 (
    echo AWS login is needed for profile ncode-sso.
    aws sso login --profile ncode-sso
    if errorlevel 1 (
        echo AWS login failed. Upload was not started.
        pause
        exit /b 1
    )
)

python upload_to_aws.py --force --profile ncode-sso
pause
