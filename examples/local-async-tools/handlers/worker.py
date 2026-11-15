#!/usr/bin/env python3
"""Deterministic external tool fixture. Replace this work with your own model call."""
import json
import os
import ssl
import sys
import time
import urllib.request

request = json.load(sys.stdin)
time.sleep(1.5)  # External work exceeds the slim launcher's one-second bound.
callback = {
    "action": "async_callback",
    "tenantId": request["tenantId"],
    "workflowId": request["workflowId"],
    "phaseId": request["phaseId"],
    "iterationId": request["iterationId"],
    "operationId": request["operationId"],
    "dispatchGeneration": request["dispatchGeneration"],
    "expectedIssuedOwnershipEpoch": request["issuedOwnershipEpoch"],
    "expectedIssuedCellId": request["issuedCellId"],
    "callbackToken": request["callbackToken"],
    "status": "COMPLETED",
    "resultPayload": {"tool": request["tool"], "approved": True},
}
body = json.dumps(callback).encode()
callback_url = request["callbackUrl"]
ssl_context = None
if callback_url.startswith("https://"):
    ca_cert = os.environ.get("RIPPFLOW_LOCAL_CA_CERT")
    if ca_cert and os.path.isfile(ca_cert):
        ssl_context = ssl.create_default_context(cafile=ca_cert)

with urllib.request.urlopen(
    urllib.request.Request(
        callback_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    ),
    timeout=5,
    context=ssl_context,
) as response:
    assert response.status == 200
