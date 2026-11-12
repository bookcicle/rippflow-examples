"""Environment-aware HTTP/HTTPS & AWS Lambda callback delivery with secret redaction."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import ssl
from typing import Any
import urllib.error
import urllib.request


_SECRET_TOKEN_RE = re.compile(
    r"(rfacb_v1_[A-Za-z0-9_\-]+|rf_tok_[A-Za-z0-9_\-]+|gh[opsu]_[A-Za-z0-9_]+|https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+)"
)


def redact_secrets(text: str) -> str:
    return _SECRET_TOKEN_RE.sub("[REDACTED_SECRET]", text)


def token_fingerprint(raw_token: str) -> str:
    cleaned = raw_token.strip()
    if not cleaned:
        return "missing"
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:8]
    return f"sha256:{digest}"


def build_callback_payload(
    request_envelope: dict[str, Any],
    *,
    status: str,
    result_payload: dict[str, Any] | None = None,
    failure_reason_code: str | None = None,
    failure_message: str | None = None,
) -> dict[str, Any]:
    if status not in {"COMPLETED", "FAILED"}:
        raise ValueError(f"Unsupported callback status: {status}")

    payload: dict[str, Any] = {
        "action": "async_callback",
        "tenantId": request_envelope["tenantId"],
        "workflowId": request_envelope["workflowId"],
        "phaseId": request_envelope["phaseId"],
        "iterationId": request_envelope["iterationId"],
        "operationId": request_envelope["operationId"],
        "dispatchGeneration": request_envelope["dispatchGeneration"],
        "expectedIssuedOwnershipEpoch": request_envelope["issuedOwnershipEpoch"],
        "expectedIssuedCellId": request_envelope["issuedCellId"],
        "callbackToken": request_envelope["callbackToken"],
        "status": status,
    }
    if status == "COMPLETED":
        payload["resultPayload"] = result_payload or {}
    else:
        payload["failureReasonCode"] = failure_reason_code or "RELEASE_SIGNOFF_REJECTED"
        payload["failureMessage"] = failure_message or "Release sign-off rejected"
    return payload


def post_callback(
    callback_url: str,
    payload: dict[str, Any],
    *,
    ca_cert_path: str | None = None,
    timeout_sec: float = 15.0,
) -> tuple[int, dict[str, Any]]:
    body_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        callback_url,
        data=body_bytes,
        headers={"Content-Type": "application/json", "Connection": "close"},
        method="POST",
    )

    ssl_context: ssl.SSLContext | None = None
    if callback_url.startswith("https://"):
        ca_file = ca_cert_path or os.environ.get("RIPPFLOW_LOCAL_CA_CERT")
        if ca_file and Path(ca_file).is_file():
            ssl_context = ssl.create_default_context(cafile=ca_file)
        else:
            ssl_context = ssl.create_default_context()

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec, context=ssl_context) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw.strip() else {"raw": raw}
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return err.code, parsed


def deliver_cloud_lambda_callback(
    callback_function_name: str,
    callback_envelope: dict[str, Any],
    *,
    status: str,
    result_payload: dict[str, Any] | None = None,
    failure_message: str | None = None,
) -> dict[str, Any]:
    """AWS Cloud callback ingress transport (`boto3.client('lambda').invoke`) for deployed Rippflow Starter cells."""
    import copy
    import boto3  # Imported lazily ONLY in deployed AWS Lambda cloud cells

    outbound = copy.deepcopy(callback_envelope)
    outbound["status"] = "SUCCEEDED" if status == "COMPLETED" else "FAILED"
    if status == "COMPLETED":
        outbound["result"] = result_payload or {}
    else:
        outbound["error"] = failure_message or "Release worker failed"

    response = boto3.client("lambda").invoke(
        FunctionName=callback_function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(outbound).encode("utf-8"),
    )
    if response.get("FunctionError") or response.get("StatusCode") != 200:
        raise RuntimeError("Cloud callback invocation was not accepted; retry required")
    accepted = json.load(response["Payload"])
    if not isinstance(accepted, dict) or accepted.get("status") != "async_callback_processed":
        raise RuntimeError("Cloud callback response did not confirm async_callback_processed")
    return {"status": "callback_accepted"}


def write_pending_signoff_envelope(
    signoff_dir: Path,
    request_envelope: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
) -> Path:
    signoff_dir.mkdir(parents=True, exist_ok=True)
    target = signoff_dir / "release_signoff.callback.json"
    record = {
        "schemaVersion": 1,
        "callbackUrl": request_envelope.get("callbackUrl"),
        "caCertPath": os.environ.get("RIPPFLOW_LOCAL_CA_CERT"),
        "tokenFingerprint": token_fingerprint(str(request_envelope.get("callbackToken", ""))),
        "metadata": metadata or {},
        "requestEnvelope": request_envelope,
    }
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    if os.name != "nt":
        os.chmod(target, 0o600)
    return target
