"""Authenticated HTTP/HTTPS Rippflow local async callback client with strict secret redaction."""

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


_SECRET_TOKEN_RE = re.compile(r"(rfacb_v1_[A-Za-z0-9_\-]+|rf_tok_[A-Za-z0-9_\-]+|gh[opsu]_[A-Za-z0-9_]+)")


def redact_secrets(text: str) -> str:
    return _SECRET_TOKEN_RE.sub("[REDACTED_TOKEN]", text)


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
    """Builds an authenticated `AsyncCallbackPayload` matching `rippflow_sdk::AsyncToolRequest`."""
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
        payload["failureReasonCode"] = failure_reason_code or "HUMAN_REVIEW_REJECTED"
        payload["failureMessage"] = failure_message or "Human reviewer rejected escalated PR shard"
    return payload


def post_callback(
    callback_url: str,
    payload: dict[str, Any],
    *,
    ca_cert_path: str | None = None,
    timeout_sec: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    """POSTs an `AsyncCallbackPayload` to Rippflow's local HTTP or HTTPS (`deploy --local`) callback ingress."""
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
            parsed = json.loads(raw) if raw.strip() else {"ok": True}
            return resp.status, parsed
    except urllib.error.HTTPError as http_err:
        raw = http_err.read().decode("utf-8", errors="replace")
        safe_raw = redact_secrets(raw)
        try:
            parsed = json.loads(safe_raw)
        except Exception:
            parsed = {"ok": False, "raw": safe_raw}
        return http_err.code, parsed


def write_pending_escalation_envelope(
    dest_path: Path,
    *,
    request_envelope: dict[str, Any],
    ca_cert_path: str | None,
    metadata: dict[str, Any],
) -> None:
    """Writes a `0600` owner-only escalation envelope so `resolve_gate.py` can complete the callback."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schemaVersion": 1,
        "callbackUrl": request_envelope["callbackUrl"],
        "caCertPath": ca_cert_path,
        "tokenFingerprint": token_fingerprint(request_envelope.get("callbackToken", "")),
        "requestEnvelope": request_envelope,
        "metadata": metadata,
    }
    tmp_path = dest_path.with_suffix(".tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
    finally:
        if tmp_path.exists():
            os.chmod(tmp_path, 0o600)
    tmp_path.replace(dest_path)
