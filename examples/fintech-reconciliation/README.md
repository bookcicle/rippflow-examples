# FinTech Statement Reconciliation

Demonstrates sharded statement parsing, math reconciliation, single-shard `rework` on `stmt-feb.pdf` (`loopCount: 0` -> re-runs `parse-statement` with `dpi: 600`), and fan-in underwriting summary.

## Run Locally from Repository Root

```sh
rippflow validate examples/fintech-reconciliation/workflow.yaml
rippflow dev examples/fintech-reconciliation/workflow.json \
  --handlers examples/fintech-reconciliation/handlers.json \
  --shards stmt-jan.pdf,stmt-feb.pdf \
  --report fintech-report.json
```

You can also scaffold a minimal reconciliation project in a new directory with `rippflow init my-app`.
