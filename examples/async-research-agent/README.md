# Durable Asynchronous AI Research Agent (`RF-AGENT-03`)

Demonstrates a long-running AI research and publication pipeline with:
1. Bounded budget and duration controls (`research-budget`, `$1.50`)
2. Durable agent context (`maxInlineBytes: 4096` with automatic artifact offloading)
3. External async tool execution (`type: async_tool`, 4h callback timeout) without holding worker compute
4. Authenticated callback settlement and explicit human publication approval (`publish-dossier`)

## Files

- `workflow.yaml` / `workflow.json` — Async research workflow definition
- `customer_async_handler.rs` — Reference JSON callback payload builder

## Validate & Inspect Controls

```sh
rippflow controls validate examples/async-research-agent/workflow.yaml --json
rippflow controls validate examples/async-research-agent/workflow.json --json
```

Against a live Controller installation:

```sh
rippflow controls inspect-context <execution-id> --json
rippflow controls inspect-operation <execution-id> publish-dossier --json
rippflow controls approve-gate <execution-id> publish-dossier --reason "Approved after compliance check" --json
rippflow controls reject-gate <execution-id> publish-dossier --reason "Needs additional primary sources" --json
```
