# Polyglot Pipeline (Rust + Python + Node.js + Java)

Runs a four-phase workflow where each phase dispatches a different language runtime over Rippflow's stdin/stdout JSON contract:

1. `ingest-packet` (`simulated.splitter`) -> Rust (`./target/debug/rust-example-handler`)
2. `parse-statement` (`simulated.ocr_extract`) -> Python (`python3 examples/python/handler.py`)
3. `verify-balance` (`simulated.balance_auditor`) -> Node.js (`node examples/node/handler.mjs`), requesting rework once on `stmt-feb.pdf`
4. `emit-underwriting-summary` (`simulated.underwriter_summary`) -> Java (`java -cp examples/java RippflowHandler`)

## Prerequisites

- `rippflow` CLI (`rippflow dev`)
- Rust (`cargo`), Python 3, Node.js, and JDK 21+

## Run Locally from Repository Root

```sh
cargo build --locked --bin rust-example-handler
javac examples/java/RippflowHandler.java
rippflow dev examples/polyglot/polyglot.workflow.json \
  --handlers examples/polyglot/polyglot.handlers.json \
  --shards stmt-jan.pdf,stmt-feb.pdf \
  --report polyglot-report.json
```

Expected: `stmt-feb.pdf` triggers one `rework` loop (`loopCount: 0` -> re-runs `parse-statement` with `dpi: 600`), then converges and completes (`status: completed`, exit code `0`).
