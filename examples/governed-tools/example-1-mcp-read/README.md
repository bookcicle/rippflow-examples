# Example 1: Simple Governed MCP Read (`example-1-mcp-read`)

Connects a read-only MCP tool (`lookup_customer_profile` on `internal-crm`) to a durable Rippflow workflow (`mcp-customer-lookup-workflow`).

## Files
- `tool.yaml` — Governed `Tool` definition (`customer-lookup` v1.0.0)
- `workflow.yaml` — Rippflow workflow pinning `customer-lookup@1.0.0`
- `request.json` — Example input payload

## Validate & Dry-Run with Rippflow CLI
```bash
rippflow tools validate examples/governed-tools/example-1-mcp-read/tool.yaml
rippflow controls validate examples/governed-tools/example-1-mcp-read/workflow.yaml
rippflow tools invoke customer-lookup \
  --input examples/governed-tools/example-1-mcp-read/request.json \
  --tool-manifest examples/governed-tools/example-1-mcp-read/tool.yaml \
  --dry-run
```
