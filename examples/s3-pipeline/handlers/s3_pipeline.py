#!/usr/bin/env python3
"""Rippflow S3 pull -> mutate -> push pipeline handler.

Demonstrates per-phase IAM role isolation across three phases:
  1. pull_from_s3 (s3.pull): Runs under STS-assumed 's3-reader' session credentials and reads
     raw shard records from S3 using boto3.
  2. mutate_records (orders.mutate): Runs in 'shared_runner_no_aws_credentials' mode with zero
     AWS credentials, enriching line items, tax, and SHA-256 content digest.
  3. push_to_s3 (s3.push): Runs under STS-assumed 's3-writer' session credentials and writes
     the mutated shard record back to S3 using boto3.
"""
import hashlib
import json
import os
import sys
import boto3
from botocore.config import Config


def s3_client():
    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 2}),
    )


def handle_pull(ctx, options, payload):
    shard = ctx.get("shard") or "default"
    bucket = options.get("bucket") or payload.get("bucket") or "app-orders"
    raw_prefix = options.get("rawPrefix") or payload.get("rawPrefix") or "raw"
    key = f"{raw_prefix}/{shard}.json"
    role_arn = os.environ.get("RIPPFLOW_EFFECTIVE_ROLE_ARN", "")
    if not role_arn.endswith(":role/s3-reader"):
        raise RuntimeError(f"s3.pull expected s3-reader role, got '{role_arn}'")
    if not os.environ.get("AWS_SESSION_TOKEN"):
        raise RuntimeError("s3.pull requires STS session token")

    client = s3_client()
    if options.get("attemptUnauthorizedWrite"):
        client.put_object(Bucket=bucket, Key=f"unauthorized/{shard}.json", Body=b"{}")

    resp = client.get_object(Bucket=bucket, Key=key)
    record = json.loads(resp["Body"].read().decode("utf-8"))
    record["shard"] = shard
    record["bucket"] = bucket
    record["sourceKey"] = key
    record["pulledWithRole"] = role_arn
    return {
        "outcome": "completed",
        "outputPointer": f"s3://{bucket}/{key}",
        "payload": record,
    }


def handle_mutate(ctx, options, payload):
    role_arn = os.environ.get("RIPPFLOW_EFFECTIVE_ROLE_ARN", "")
    if role_arn != "minimal-default-role":
        raise RuntimeError(f"orders.mutate must run with minimal-default-role, got '{role_arn}'")
    for cred_var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        if os.environ.get(cred_var):
            raise RuntimeError(f"orders.mutate leaked credential env var {cred_var}")

    if options.get("attemptUnauthorizedS3"):
        s3_client().get_object(Bucket="app-orders", Key="raw/a.json")

    record = dict(payload or {})
    tax_rate_bps = int(options.get("taxRateBps", 825))
    currency = options.get("currency", "USD")
    items = []
    subtotal_cents = 0
    for item in record.get("items", []):
        qty = int(item["quantity"])
        unit_cents = int(item["unitPriceCents"])
        line_cents = qty * unit_cents
        subtotal_cents += line_cents
        items.append(
            {
                "sku": str(item["sku"]).strip().upper(),
                "quantity": qty,
                "unitPriceCents": unit_cents,
                "lineTotalCents": line_cents,
            }
        )
    tax_cents = (subtotal_cents * tax_rate_bps + 5000) // 10000
    total_cents = subtotal_cents + tax_cents
    canonical = json.dumps(
        {
            "orderId": record.get("orderId"),
            "shard": record.get("shard"),
            "items": items,
            "subtotalCents": subtotal_cents,
            "taxCents": tax_cents,
            "totalCents": total_cents,
            "currency": currency,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    mutated = {
        "orderId": record.get("orderId"),
        "shard": record.get("shard"),
        "bucket": record.get("bucket", "app-orders"),
        "sourceKey": record.get("sourceKey"),
        "pulledWithRole": record.get("pulledWithRole"),
        "mutatedInPhase": ctx.get("phase"),
        "mutateIsolationRole": role_arn,
        "status": "mutated",
        "currency": currency,
        "items": items,
        "subtotalCents": subtotal_cents,
        "taxCents": tax_cents,
        "totalCents": total_cents,
        "contentSha256": digest,
    }
    return {
        "outcome": "completed",
        "outputPointer": ctx.get("inputPointer"),
        "payload": mutated,
    }


def handle_push(ctx, options, payload):
    shard = ctx.get("shard") or "default"
    record = dict(payload or {})
    bucket = options.get("bucket") or record.get("bucket") or "app-orders"
    processed_prefix = options.get("processedPrefix") or "processed"
    key = f"{processed_prefix}/{shard}.json"
    role_arn = os.environ.get("RIPPFLOW_EFFECTIVE_ROLE_ARN", "")
    if not role_arn.endswith(":role/s3-writer"):
        raise RuntimeError(f"s3.push expected s3-writer role, got '{role_arn}'")
    if not os.environ.get("AWS_SESSION_TOKEN"):
        raise RuntimeError("s3.push requires STS session token")

    record["pushedWithRole"] = role_arn
    record["outputKey"] = key
    body = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
    put_resp = s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
    )
    record["etag"] = str(put_resp.get("ETag", "")).strip('"')
    return {
        "outcome": "completed",
        "outputPointer": f"s3://{bucket}/{key}",
        "payload": record,
    }


def main():
    ctx = json.loads(sys.stdin.read())
    service = ctx.get("service")
    options = ctx.get("options") or {}
    payload = ctx.get("payload") or {}
    if service == "s3.pull":
        out = handle_pull(ctx, options, payload)
    elif service == "orders.mutate":
        out = handle_mutate(ctx, options, payload)
    elif service == "s3.push":
        out = handle_push(ctx, options, payload)
    else:
        raise RuntimeError(f"unknown service '{service}'")
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
