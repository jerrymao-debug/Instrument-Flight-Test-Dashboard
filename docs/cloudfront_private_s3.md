# CloudFront Private S3 Hosting

The raw S3 dashboard URL can fail with `AccessDenied` when bucket-level Block Public Access is reset to the secure default. The durable fix is to serve the static dashboard through CloudFront while keeping S3 private.

## Requested AWS Setup

S3 bucket:

```text
vibration-data-daq
```

S3 dashboard prefix:

```text
insturment_fly_test_dashboard_code/*
```

CloudFront settings:

- Origin domain: `vibration-data-daq.s3.us-west-2.amazonaws.com`
- Origin path: `/insturment_fly_test_dashboard_code`
- Default root object: `index.html`
- Origin Access Control: S3, SigV4, signing always
- Viewer protocol policy: redirect HTTP to HTTPS
- Allowed methods: `GET`, `HEAD`
- CloudFront default certificate is acceptable until a company DNS name is assigned.

Bucket policy statement:

```json
{
  "Sid": "AllowInstrumentDashboardCloudFrontRead",
  "Effect": "Allow",
  "Principal": {
    "Service": "cloudfront.amazonaws.com"
  },
  "Action": "s3:GetObject",
  "Resource": "arn:aws:s3:::vibration-data-daq/insturment_fly_test_dashboard_code/*",
  "Condition": {
    "StringEquals": {
      "AWS:SourceArn": "arn:aws:cloudfront::149938346436:distribution/<distribution-id>"
    }
  }
}
```

After CloudFront is configured, S3 Block Public Access can stay fully enabled:

```text
BlockPublicAcls=true
IgnorePublicAcls=true
BlockPublicPolicy=true
RestrictPublicBuckets=true
```

## Dashboard Code Change After CloudFront Exists

Set the public base URL to the CloudFront domain, without a trailing slash:

```text
DASHBOARD_PUBLIC_BASE_URL=https://<cloudfront-domain>
```

Then run the builder or let the Databricks refresh loop run. The dashboard HTML, mission links, and download buttons will use CloudFront URLs instead of raw S3 URLs.

Manual rebuild example:

```powershell
python .\dashboard\dashboard_builder.py --force --profile ncode-sso --public-base-url "https://<cloudfront-domain>"
```

Databricks:

```python
import os
os.environ["DASHBOARD_PUBLIC_BASE_URL"] = "https://<cloudfront-domain>"
```

The Databricks runner downloads `dashboard_builder.py` from S3 every cycle, so the latest builder code is used automatically.
