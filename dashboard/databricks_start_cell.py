# Databricks notebook source
# Paste/run this one cell in Databricks to start the 10-minute dashboard refresh loop.

dbutils.fs.cp(
    "s3://vibration-data-daq/insturment_fly_test_dashboard_code/databricks_dashboard_refresh.py",
    "file:/tmp/databricks_dashboard_refresh.py",
    True,
)

exec(open("/tmp/databricks_dashboard_refresh.py", "r", encoding="utf-8").read())
