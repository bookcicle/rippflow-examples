# Example 2: Approval-Protected Business Action (`example-2-approval-crm-action`)

Demonstrates a state-changing MCP tool (`update_customer` on `internal-crm`) governed by:
- Required idempotency (`mode: required`, `key: workflow-operation-id`)
- Explicit permission scope (`crm.customer.write`)
- Pre-dispatch human approval (`approval: required`) bound to the exact `inputDigest` and `toolVersion`
- Hard budget reservation (`budget: crm-operations`, `maxCostPerCall: 0.05`)
- Concurrency cap (`maxConcurrent: 10`)

## Files
- `tool.yaml` — Governed `Tool` definition (`crm-update` v1.0.0)
- `workflow.yaml` — Rippflow workflow (`approval-crm-update-workflow`)
- `request.json` — Example input payload

## Validate & Dry-Run with Rippflow CLI
```bash
rippflow tools validate examples/governed-tools/example-2-approval-crm-action/tool.yaml
rippflow controls validate examples/governed-tools/example-2-approval-crm-action/workflow.yaml
rippflow tools invoke crm-update \
  --input examples/governed-tools/example-2-approval-crm-action/request.json \
  --tool-manifest examples/governed-tools/example-2-approval-crm-action/tool.yaml \
  --dry-run
```
