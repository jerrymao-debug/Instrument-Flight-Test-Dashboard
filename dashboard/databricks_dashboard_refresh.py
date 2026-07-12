# Databricks notebook source
# Paste this file into the Databricks notebook, or upload it as a notebook script.
# It checks S3 every 10 minutes and only uploads a new dashboard when source data changed.

# COMMAND ----------

import subprocess
import sys
import time
from datetime import datetime


BUILDER_S3_URI = "s3://vibration-data-daq/insturment_fly_test_dashboard_code/dashboard_builder.py"
LOCAL_BUILDER = "/tmp/instrument_dashboard_builder.py"
CACHE_DIR = "/dbfs/tmp/instrument_dashboard_cache"
OUTPUT_DIR = "/dbfs/tmp/instrument_dashboard_site"
RUN_EVERY_SECONDS = 600


def copy_builder_from_s3() -> None:
    # dbutils is available inside Databricks notebooks.
    dbutils.fs.cp(BUILDER_S3_URI, f"file:{LOCAL_BUILDER}", True)  # noqa: F821


def run_builder_once() -> int:
    copy_builder_from_s3()
    command = [
        sys.executable,
        LOCAL_BUILDER,
        "--cache-dir",
        CACHE_DIR,
        "--output-dir",
        OUTPUT_DIR,
    ]
    result = subprocess.run(command, text=True, capture_output=True)
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] dashboard refresh")
    print(result.stdout)
    if result.stderr:
        print("STDERR:")
        print(result.stderr)
    return result.returncode


while True:
    exit_code = run_builder_once()
    if exit_code != 0:
        print(f"Refresh failed with exit code {exit_code}. Will try again in {RUN_EVERY_SECONDS} seconds.")
    time.sleep(RUN_EVERY_SECONDS)
