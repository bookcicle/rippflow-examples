# Example 3: Long-Running AI Research Workflow (`example-3-long-running-research`)

Builds on **RF-AGENT-03** by combining:
1. Durable agent context (`DurableAgentContext`) with S3 artifact offloading for large payloads (`> 4 KiB`)
2. Governed MCP corpus search calls (`research-corpus-search@1.0.0`)
3. Asynchronous deep synthesis (`async_tool` adapter with authenticated callback)
4. Approval-gated final dossier publication (`research-dossier-publish@1.0.0`)
5. Worker crash recovery and budget settlement across iterations

## Files
- `search-tool.yaml` — Governed MCP search tool (`research-corpus-search` v1.0.0)
- `publish-tool.yaml` — Approval-protected MCP dossier publication tool (`research-dossier-publish` v1.0.0)
- `workflow.yaml` — Multi-phase governed + async workflow (`long-running-governed-research-workflow`)
- `request.json` — Initial query input

## Validate with Rippflow CLI
```bash
rippflow tools validate examples/governed-tools/example-3-long-running-research/search-tool.yaml
rippflow tools validate examples/governed-tools/example-3-long-running-research/publish-tool.yaml
rippflow controls validate examples/governed-tools/example-3-long-running-research/workflow.yaml
```
