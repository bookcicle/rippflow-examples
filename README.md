# Rippflow Public Examples

Official public example repository for [Rippflow](https://rippflow.bookcicle.com). Every workflow, handler, and governed tool recipe here runs locally with the `rippflow` CLI (`rippflow dev`, `rippflow validate`, `rippflow controls validate`, `rippflow tools validate`, `rippflow tools invoke --dry-run`).

## Prerequisites

- **`rippflow` CLI** installed and on `PATH`
- **Python 3** (required by most example handlers)
- **Rust (`cargo`), Node.js, and JDK 21+** (for the polyglot and native Rust handler examples)

Build the simulated Rust handler binary and compile the Java handler once from the repository root:

```sh
cargo build --locked --bin rust-example-handler
javac examples/java/RippflowHandler.java
```

## Example Catalog

| Directory | Description | Primary CLI Command |
|---|---|---|
| [`examples/polyglot/`](examples/polyglot/README.md) | Four-language workflow (Rust, Python, Node.js, Java) with shard rework | `rippflow dev examples/polyglot/polyglot.workflow.json --handlers examples/polyglot/polyglot.handlers.json --shards stmt-jan.pdf,stmt-feb.pdf` |
| [`examples/media/`](examples/media/README.md) | Media segment transcoding, loudness QA rework, and HLS fan-in | `rippflow dev examples/media/media.workflow.json --handlers examples/media/media.handlers.json --shards scene-01.mp4,scene-02.mp4` |
| [`examples/resume/`](examples/resume/README.md) | Eight-phase parallel branching and converging DAG | `rippflow dev examples/resume/resume.workflow.json --handlers examples/resume/resume.handlers.json` |
| [`examples/failure/`](examples/failure/README.md) | Transient rework recovery (`recoverable-item.bin`) and fail-closed crash (`fatal-crash.bin`) | `rippflow dev examples/failure/failure.workflow.json --handlers examples/failure/failure.handlers.json --shards ok-item.bin,recoverable-item.bin` |
| [`examples/fintech-reconciliation/`](examples/fintech-reconciliation/README.md) | Statement reconciliation with DPI rework loop | `rippflow dev examples/fintech-reconciliation/workflow.json --handlers examples/fintech-reconciliation/handlers.json --shards stmt-jan.pdf,stmt-feb.pdf` |
| [`examples/hello/`](examples/hello/README.md) | Minimal single-phase Python starter matching the quickstart docs | `rippflow validate examples/hello/workflow.yaml --handlers examples/hello/handlers.json` |
| [`examples/governed-tools/`](examples/governed-tools/) | Governed MCP reads, approval-gated CRM writes, and budgeted research | `rippflow tools validate examples/governed-tools/example-1-mcp-read/tool.yaml` |
| [`examples/async-research-agent/`](examples/async-research-agent/README.md) | Durable `async_tool` callback and control-policy workflow | `rippflow controls validate examples/async-research-agent/workflow.yaml --json` |
| [`examples/async-callback/`](examples/async-callback/README.md) | Authenticated async callback adapter and HMAC test suite | `python3 -m unittest discover -s examples/async-callback -p 'test_*.py'` |
| [`examples/scoped-iam/`](examples/scoped-iam/README.md) | Least-privilege STS phase role template and workflow validation | `rippflow validate examples/scoped-iam/workflow.yaml` |
| [`examples/s3-pipeline/`](examples/s3-pipeline/README.md) | Scoped S3 pull/mutate/push subprocess pipeline | `rippflow validate examples/s3-pipeline/workflow.yaml --handlers examples/s3-pipeline/handlers.json` |
| [`examples/local-async-tools/`](examples/local-async-tools/README.md) | Project-mode async tool orchestration with `rippflow.yaml` | `rippflow validate --project examples/local-async-tools/rippflow.yaml --json` |
| [`examples/multi-model-review/`](examples/multi-model-review/README.md) | Project-mode multi-reviewer fan-out and human gate | `rippflow validate --project examples/multi-model-review/rippflow.yaml --json` |
| [`examples/edge-native/`](examples/edge-native/README.md) | Native Rust edge workflows (sensor, vision, document, recovery) | `cargo build --locked -p rippflow-edge-example-handler` |
| [`examples/native-handlers/`](examples/native-handlers/Cargo.toml) | Standalone Rust edge example handlers crate | `cargo build --locked --bins` |
| [`examples/pr-pipeline/`](examples/pr-pipeline/README.md) | Autonomous PR validation and fix pipeline | See directory README |
| [`examples/nightwatch/`](examples/nightwatch/README.md) | Nightly repository maintenance and audit workflow | See directory README |
| [`examples/release-train/`](examples/release-train/README.md) | Coordinated multi-repo release train workflow | See directory README |
| [`plugins/`](plugins/README.md) | Chat agent plugins proving CLI, Python stdio, and Node.js stdio transports | `python3 -m unittest discover -s plugins -p "test_*.py"` |
