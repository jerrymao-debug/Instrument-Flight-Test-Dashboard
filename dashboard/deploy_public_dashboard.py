from __future__ import annotations

import argparse
import json
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError


AWS_PROFILE = "ncode-sso"
AWS_REGION = "us-west-2"
BUCKET = "vibration-data-daq"
SITE_PREFIX = "insturment_fly_test_dashboard_code"
OAC_NAME = "instrument-flight-dashboard-public-oac"
DISTRIBUTION_COMMENT = "Instrument Flight Test Dashboard public"
ORIGIN_ID = "instrument-flight-dashboard-public-s3-origin"
FALLBACK_OAI_COMMENT = "corpS3origin"

# AWS managed cache policy: CachingDisabled. This keeps uploads visible quickly.
CACHE_POLICY_ID = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"


def aws_session(profile: str | None) -> boto3.Session:
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return boto3.Session(region_name=AWS_REGION)
    if profile:
        return boto3.Session(profile_name=profile, region_name=AWS_REGION)
    return boto3.Session(region_name=AWS_REGION)


def find_oac(cloudfront) -> str | None:
    paginator = cloudfront.get_paginator("list_origin_access_controls")
    for page in paginator.paginate():
        for item in page.get("OriginAccessControlList", {}).get("Items", []):
            if item.get("Name") == OAC_NAME:
                return item["Id"]
    return None


def ensure_oac(cloudfront) -> str | None:
    existing = find_oac(cloudfront)
    if existing:
        return existing
    try:
        response = cloudfront.create_origin_access_control(
            OriginAccessControlConfig={
                "Name": OAC_NAME,
                "Description": "Public CloudFront access to private S3 dashboard files.",
                "SigningProtocol": "sigv4",
                "SigningBehavior": "always",
                "OriginAccessControlOriginType": "s3",
            }
        )
        return response["OriginAccessControl"]["Id"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "AccessDenied":
            raise
        return None


def find_oai(cloudfront) -> tuple[str, str] | None:
    paginator = cloudfront.get_paginator("list_cloud_front_origin_access_identities")
    for page in paginator.paginate():
        for item in page.get("CloudFrontOriginAccessIdentityList", {}).get("Items", []):
            if item.get("Comment") == FALLBACK_OAI_COMMENT:
                return item["Id"], item["S3CanonicalUserId"]
    return None


def find_distribution(cloudfront) -> tuple[str, str] | None:
    paginator = cloudfront.get_paginator("list_distributions")
    for page in paginator.paginate():
        for item in page.get("DistributionList", {}).get("Items", []):
            if item.get("Comment") == DISTRIBUTION_COMMENT:
                return item["Id"], item["DomainName"]
    return None


def distribution_config(oac_id: str | None, oai_id: str | None, caller_reference: str) -> dict:
    s3_origin_config = {
        "OriginAccessIdentity": f"origin-access-identity/cloudfront/{oai_id}" if oai_id else ""
    }
    origin = {
        "Id": ORIGIN_ID,
        "DomainName": f"{BUCKET}.s3.{AWS_REGION}.amazonaws.com",
        "OriginPath": f"/{SITE_PREFIX}",
        "S3OriginConfig": s3_origin_config,
    }
    if oac_id:
        origin["OriginAccessControlId"] = oac_id
    return {
        "CallerReference": caller_reference,
        "Comment": DISTRIBUTION_COMMENT,
        "Enabled": True,
        "DefaultRootObject": "index.html",
        "Origins": {
            "Quantity": 1,
            "Items": [origin],
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
            "FunctionAssociations": {"Quantity": 0},
        },
        "CustomErrorResponses": {"Quantity": 0},
        "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
        "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
        "HttpVersion": "http2",
        "IsIPV6Enabled": True,
        "PriceClass": "PriceClass_100",
    }


def ensure_distribution(cloudfront, oac_id: str | None, oai_id: str | None) -> tuple[str, str, str]:
    existing = find_distribution(cloudfront)
    if not existing:
        response = cloudfront.create_distribution(
            DistributionConfig=distribution_config(oac_id, oai_id, str(time.time_ns()))
        )
        dist = response["Distribution"]
        return dist["Id"], dist["DomainName"], dist["Status"]

    dist_id, domain = existing
    response = cloudfront.get_distribution_config(Id=dist_id)
    config = response["DistributionConfig"]
    etag = response["ETag"]
    updated = distribution_config(oac_id, oai_id, config["CallerReference"])
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


def ensure_bucket_policy(s3, account_id: str, distribution_id: str, oai_canonical_user_id: str | None) -> None:
    policy = get_bucket_policy(s3)
    statements = policy.setdefault("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
        policy["Statement"] = statements

    sid = "AllowInstrumentPublicDashboardCloudFrontRead"
    statements[:] = [statement for statement in statements if statement.get("Sid") != sid]
    if oai_canonical_user_id:
        statements.append(
            {
                "Sid": sid,
                "Effect": "Allow",
                "Principal": {"CanonicalUser": oai_canonical_user_id},
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{BUCKET}/{SITE_PREFIX}/*",
            }
        )
    else:
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


def create_invalidation(cloudfront, distribution_id: str) -> str:
    response = cloudfront.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            "CallerReference": str(time.time_ns()),
            "Paths": {"Quantity": 1, "Items": ["/*"]},
        },
    )
    return response["Invalidation"]["Id"]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy public CloudFront access for the dashboard.")
    parser.add_argument("--profile", default=AWS_PROFILE)
    parser.add_argument("--wait", action="store_true", help="Wait for the CloudFront distribution to finish deploying.")
    parser.add_argument("--invalidate", action="store_true", help="Invalidate CloudFront after updating.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    session = aws_session(args.profile)
    sts = session.client("sts")
    account_id = sts.get_caller_identity()["Account"]
    cloudfront = session.client("cloudfront", region_name="us-east-1")
    s3 = session.client("s3", region_name=AWS_REGION)

    oac_id = ensure_oac(cloudfront)
    oai_id = None
    oai_canonical_user_id = None
    if not oac_id:
        fallback_oai = find_oai(cloudfront)
        if not fallback_oai:
            raise RuntimeError(f"Cannot create OAC and no fallback OAI named {FALLBACK_OAI_COMMENT!r} exists.")
        oai_id, oai_canonical_user_id = fallback_oai
    distribution_id, domain_name, status = ensure_distribution(cloudfront, oac_id, oai_id)
    ensure_bucket_policy(s3, account_id, distribution_id, oai_canonical_user_id)
    invalidation_id = create_invalidation(cloudfront, distribution_id) if args.invalidate else None

    if args.wait:
        waiter = cloudfront.get_waiter("distribution_deployed")
        waiter.wait(Id=distribution_id)
        status = "Deployed"

    result = {
        "url": f"https://{domain_name}/",
        "distribution_id": distribution_id,
        "status": status,
        "invalidation_id": invalidation_id,
        "origin_access": "OAC" if oac_id else f"OAI {oai_id}",
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
