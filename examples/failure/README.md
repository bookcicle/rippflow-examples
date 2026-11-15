# Failure & Retry Semantics

Demonstrates both bounded rework recovery and fail-closed worker crash behavior with `rippflow dev`:

1. **`recoverable-item.bin`** returns `rework` on `audit-item` at `loopCount: 0` (`override_inputs: { "strict": false, "fallback_mode": true }`), re-runs `process-item`, passes `audit-item` on `loopCount: 1`, and completes:

```sh
rippflow dev examples/failure/failure.workflow.json \
  --handlers examples/failure/failure.handlers.json \
  --shards ok-item.bin,recoverable-item.bin \
  --report failure-recovery-report.json
```

2. **`fatal-crash.bin`** intentionally crashes in `process-item`, exhausts `sla.max_attempts: 2`, and fails closed (exit code `1`):

```sh
rippflow dev examples/failure/failure.workflow.json \
  --handlers examples/failure/failure.handlers.json \
  --shards ok-item.bin,fatal-crash.bin
```
