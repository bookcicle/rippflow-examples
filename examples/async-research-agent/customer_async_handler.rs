//! Standalone reference payload builder for `type: async_tool` (`RF-AGENT-03`).
//!
//! Reads the JSON dispatch request passed to an asynchronous handler and builds the
//! callback payload structure posted back to the Rippflow callback endpoint with
//! `rfacb_v1_<hmac>` authentication.

use serde_json::{json, Value};

pub fn handle_async_research_invocation(req: &Value) -> Value {
    let topic = req
        .get("inputs")
        .and_then(|v| v.get("topic"))
        .and_then(Value::as_str)
        .unwrap_or("quarterly SEC 10-K risk factor synthesis");

    if req
        .get("inputs")
        .and_then(|v| v.get("simulateUpstreamError"))
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return json!({
            "invocationId": req.get("invocationId").cloned().unwrap_or(Value::Null),
            "dispatchGeneration": req.get("dispatchGeneration").cloned().unwrap_or(json!(1)),
            "status": "failed",
            "error": {
                "code": "UPSTREAM_PROVIDER_RATE_LIMITED",
                "message": "Model provider returned 429 after retry budget"
            },
            "usage": {
                "resource": "model_cost",
                "unit": "micro_usd",
                "amount": 150000
            }
        });
    }

    let tenant_id = req
        .get("tenantId")
        .and_then(Value::as_str)
        .unwrap_or("tenant");
    let workflow_id = req
        .get("workflowId")
        .and_then(Value::as_str)
        .unwrap_or("workflow");
    let gen = req
        .get("dispatchGeneration")
        .and_then(Value::as_u64)
        .unwrap_or(1);

    json!({
        "invocationId": req.get("invocationId").cloned().unwrap_or(Value::Null),
        "dispatchGeneration": gen,
        "status": "completed",
        "result": {
            "topic": topic,
            "invocationId": req.get("invocationId").cloned().unwrap_or(Value::Null),
            "idempotencyKey": req.get("idempotencyKey").cloned().unwrap_or(Value::Null),
            "findingsSummary": "Identified 4 material supply-chain disclosures and 2 regulatory contingencies.",
            "citationsCount": 18,
            "artifactRef": format!(
                "s3://customer-research-artifacts/{tenant_id}/{workflow_id}/gen-{gen}/report.json"
            )
        },
        "usage": {
            "resource": "model_cost",
            "unit": "micro_usd",
            "amount": 920000
        }
    })
}
