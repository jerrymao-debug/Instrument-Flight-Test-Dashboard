from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


AWS_PROFILE = "ncode-sso"
AWS_REGION = "us-west-2"
BUCKET = "vibration-data-daq"
SITE_PREFIX = "insturment_fly_test_dashboard_code"
FUNCTION_NAME = "instrument-flight-dashboard-basic-auth"
OAC_NAME = "instrument-flight-dashboard-oac"
DISTRIBUTION_COMMENT = "Instrument Flight Test Dashboard password protected"
ORIGIN_ID = "instrument-flight-dashboard-s3-origin"
CREDENTIAL_FILE = Path(__file__).resolve().with_name("password_protected_dashboard_credentials.json")

# AWS managed cache policy: CachingDisabled. This keeps dashboard uploads visible quickly.
CACHE_POLICY_ID = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"


def aws_session(profile: str | None) -> boto3.Session:
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return boto3.Session(region_name=AWS_REGION)
    if profile:
        return boto3.Session(profile_name=profile, region_name=AWS_REGION)
    return boto3.Session(region_name=AWS_REGION)


def load_or_create_credentials(username: str, password: str | None, force_new: bool) -> tuple[str, str]:
    if password is None:
        password = os.environ.get("DASHBOARD_PASSWORD")
    username = os.environ.get("DASHBOARD_USERNAME", username)

    if password is None and CREDENTIAL_FILE.exists() and not force_new:
        saved = json.loads(CREDENTIAL_FILE.read_text(encoding="utf-8"))
        username = saved["username"]
        password = saved["password"]

    if password is None:
        password = secrets.token_urlsafe(24)

    if ":" in username:
        raise ValueError("Username cannot contain ':' for Basic Auth.")
    if ":" in password:
        raise ValueError("Password cannot contain ':' for Basic Auth.")

    CREDENTIAL_FILE.write_text(
        json.dumps({"username": username, "password": password}, indent=2),
        encoding="utf-8",
    )
    return username, password


def function_code(username: str, password: str) -> bytes:
    expected = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    code = f"""function handler(event) {{
  var request = event.request;
  var headers = request.headers;
  var expected = "Basic {expected}";

  if (headers.authorization && headers.authorization.value === expected) {{
    return request;
  }}

  return {{
    statusCode: 401,
    statusDescription: "Unauthorized",
    headers: {{
      "www-authenticate": {{ value: "Basic realm=\\"Instrument Flight Dashboard\\"" }},
      "cache-control": {{ value: "no-store" }}
    }},
    body: "Authentication required"
  }};
}}
"""
    return code.encode("utf-8")


def publish_auth_function(cloudfront, username: str, password: str) -> str:
    config = {
        "Comment": "Shared-password gate for the instrument flight dashboard.",
        "Runtime": "cloudfront-js-2.0",
    }
    code = function_code(username, password)
    try:
        response = cloudfront.describe_function(Name=FUNCTION_NAME, Stage="DEVELOPMENT")
        etag = response["ETag"]
        response = cloudfront.update_function(
            Name=FUNCTION_NAME,
            IfMatch=etag,
            FunctionConfig=config,
            FunctionCode=code,
        )
        etag = response["ETag"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchFunctionExists":
            raise
        response = cloudfront.create_function(
            Name=FUNCTION_NAME,
            FunctionConfig=config,
            FunctionCode=code,
        )
        etag = response["ETag"]

    response = cloudfront.publish_function(Name=FUNCTION_NAME, IfMatch=etag)
    return response["FunctionSummary"]["FunctionMetadata"]["FunctionARN"]


def find_oac(cloudfront) -> str | None:
    paginator = cloudfront.get_paginator("list_origin_access_controls")
    for page in paginator.paginate():
        for item in page.get("OriginAccessControlList", {}).get("Items", []):
            if item.get("Name") == OAC_NAME:
                return item["Id"]
    return None


def ensure_oac(cloudfront) -> str:
    existing = find_oac(cloudfront)
    if existing:
        return existing
    response = cloudfront.create_origin_access_control(
        OriginAccessControlConfig={
            "Name": OAC_NAME,
            "Description": "Private S3 access for the instrument flight dashboard.",
            "SigningProtocol": "sigv4",
            "SigningBehavior": "always",
            "OriginAccessControlOriginType": "s3",
        }
    )
    return response["OriginAccessControl"]["Id"]


def find_distribution(cloudfront) -> tuple[str, str] | None:
    paginator = cloudfront.get_paginator("list_distributions")
    for page in paginator.paginate():
        for item in page.get("DistributionList", {}).get("Items", []):
            if item.get("Comment") == DISTRIBUTION_COMMENT:
                return item["Id"], item["DomainName"]
    return None


def distribution_config(function_arn: str, oac_id: str, caller_reference: str) -> dict:
    return {
        "CallerReference": caller_reference,
        "Comment": DISTRIBUTION_COMMENT,
        "Enabled": True,
        "DefaultRootObject": "index.html",
        "Origins": {
            "Quantity": 1,
            "Items": [
                {
                    "Id": ORIGIN_ID,
                    "DomainName": f"{BUCKET}.s3.{AWS_REGION}.amazonaws.com",
                    "OriginPath": f"/{SITE_PREFIX}",
                    "S3OriginConfig": {"OriginAccessIdentity": ""},
                    "OriginAccessControlId": oac_id,
                }
            ],
        },
        "DefaultCacheBehavior": {
            "TargetOriginId": ORIGIN_ID,
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {
                "Quantity": 2,
                "Items": ["GET", "HEAD"],
                "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
            },
            "CachePolicyId": CACHE_POLICY_ID,
            "Compress": True,
            "TrustedSigners": {"Enabled": False, "Quantity": 0},
            "TrustedKeyGroups": {"Enabled": False, "Quantity": 0},
            "FunctionAssociations": {
                "Quantity": 1,
                "Items": [{"EventType": "viewer-request", "FunctionARN": function_arn}],
            },
        },
        "CustomErrorResponses": {"Quantity": 0},
        "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
        "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
        "HttpVersion": "http2",
        "IsIPV6Enabled": True,
        "PriceClass": "PriceClass_100",
    }


def ensure_distribution(cloudfront, function_arn: str, oac_id: str) -> tuple[str, str, str]:
    existing = find_distribution(cloudfront)
    if not existing:
        response = cloudfront.create_distribution(
            DistributionConfig=distribution_config(function_arn, oac_id, str(time.time_ns()))
        )
        dist = response["Distribution"]
        return dist["Id"], dist["DomainName"], dist["Status"]

    dist_id, domain = existing
    response = cloudfront.get_distribution_config(Id=dist_id)
    config = response["DistributionConfig"]
    etag = response["ETag"]
    caller_reference = config["CallerReference"]
    updated = distribution_config(function_arn, oac_id, caller_reference)

    response = cloudfront.update_distribution(
        Id=dist_id,
        IfMatch=etag,
        DistributionConfig=updated,
    )
    dist = response["Distribution"]
    return dist["Id"], dist["DomainName"], dist["Status"]


def get_bucket_policy(s3) -> dict:
    try:
        response = s3.get_bucket_policy(Bucket=BUCKET)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchBucketPolicy", "NoSuchBucket"}:
            return {"Version": "2012-10-17", "Statement": []}
        raise
    return json.loads(response["Policy"])


def ensure_bucket_policy(s3, account_id: str, distribution_id: str) -> None:
    policy = get_bucket_policy(s3)
    statements = policy.setdefault("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
        policy["Statement"] = statements

    sid = "AllowInstrumentDashboardCloudFrontRead"
    statements[:] = [statement for statement in statements if statement.get("Sid") != sid]
    statements.append(
        {
            "Sid": sid,
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{BUCKET}/{SITE_PREFIX}/*",
            "Condition": {
                "StringEquals": {
                    "AWS:SourceArn": f"arn:aws:cloudfront::{account_id}:distribution/{distribution_id}"
                }
            },
        }
    )
    s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps(policy, separators=(",", ":")))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy password-protected CloudFront access for the dashboard.")
    parser.add_argument("--profile", default=AWS_PROFILE)
    parser.add_argument("--username", default="dashboard")
    parser.add_argument("--password", default=None, help="Avoid passing secrets on shared terminals; DASHBOARD_PASSWORD is preferred.")
    parser.add_argument("--new-password", action="store_true", help="Generate and save a new random password.")
    parser.add_argument("--wait", action="store_true", help="Wait for the CloudFront distribution to finish deploying.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    session = aws_session(args.profile)
    sts = session.client("sts")
    account_id = sts.get_caller_identity()["Account"]
    cloudfront = session.client("cloudfront", region_name="us-east-1")
    s3 = session.client("s3", region_name=AWS_REGION)

    username, password = load_or_create_credentials(args.username, args.password, args.new_password)
    function_arn = publish_auth_function(cloudfront, username, password)
    oac_id = ensure_oac(cloudfront)
    distribution_id, domain_name, status = ensure_distribution(cloudfront, function_arn, oac_id)
    ensure_bucket_policy(s3, account_id, distribution_id)

    if args.wait:
        waiter = cloudfront.get_waiter("distribution_deployed")
        waiter.wait(Id=distribution_id)
        status = "Deployed"

    result = {
        "url": f"https://{domain_name}/",
        "distribution_id": distribution_id,
        "status": status,
        "username": username,
        "credential_file": str(CREDENTIAL_FILE),
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
