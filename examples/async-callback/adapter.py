"""Pure example adapter: Lambda Event dispatch -> authenticated starter callback.

The adapter role needs lambda:InvokeFunction only on its dedicated callback-only
ingress ARN, never the full internal Starter API.
For side effects, enforce event['idempotencyKey'] in the provider BEFORE executing.
Lambda Event delivery can repeat; this pure computation is repeatable.
"""
import copy
import json

import boto3


def lambda_handler(event, context):
    callback = copy.deepcopy(event["callback"]["envelope"])
    callback["status"] = "SUCCEEDED"
    callback["result"] = {"answer": event.get("input"), "invocationId": event["invocationId"]}
    response = boto3.client("lambda").invoke(
        FunctionName=event["callback"]["functionName"],
        InvocationType="RequestResponse",
        Payload=json.dumps(callback).encode(),
    )
    if response.get("FunctionError") or response.get("StatusCode") != 200:
        # Lambda Event retry replays the same scoped completion safely. Do not log
        # callback envelopes, tokens, or raw invocation payloads.
        raise RuntimeError("callback was not accepted; retry required")
    accepted = json.load(response["Payload"])
    if not isinstance(accepted, dict) or accepted.get("status") != "async_callback_processed":
        raise RuntimeError("callback response did not confirm acceptance; retry required")
    return {"status": "callback_accepted"}
