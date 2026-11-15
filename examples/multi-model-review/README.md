# Bounded Multi-Model Build & Review Workflow

Canonical Rippflow local orchestration project demonstrating slim `async_tool` launchers, concurrent independent reviewer callbacks, dual-reviewer fan-in barriers, bounded rework loops (`max_loops: 9`, up to 10 total rounds), and fail-closed `REWORK_LIMIT_EXHAUSTED` termination.

## Workflow Topology (`workflows/review.yaml`)

1. **`build` (`async_tool`)** — Slim launcher hands off build work to an external worker and exits immediately (`{"outcome":"dispatched"}`), releasing its local worker slot while awaiting the authenticated `rfacb_v1_...` callback.
2. **`publish_pr` (`sync_lambda`)** — Publishes/updates the PR metadata for the current round (`requires: [build]`).
3. **`reviewer_a` & `reviewer_b` (`async_tool`, parallel fan-out)** — Two independent model reviewers run concurrently (`requires: [publish_pr]`) and deliver authenticated callbacks out of order across rounds (`reviewer_b` before `reviewer_a` on even rounds, `reviewer_a` before `reviewer_b` on odd rounds).
4. **`decision` (`sync_lambda`, fan-in barrier)** — Joins `requires: [reviewer_a, reviewer_b]`:
   - `approved` -> advances to `human_gate` only when **both** reviewers approve.
   - `changes_requested` -> `rerun_phase: build` with `max_loops: 9` (up to 10 rounds total: initial round `0` + 9 rework rounds `1..=9`). If round `9` still requests changes, the engine fails closed with `REWORK_LIMIT_EXHAUSTED` without ever entering `human_gate`.
5. **`human_gate` (`sync_lambda`, `terminal: true`)** — Reached once unanimous reviewer convergence is achieved.

## Quickstart (CLI Root)

```sh
# 1. Validate canonical project
rippflow validate --project examples/multi-model-review/rippflow.yaml --json

# 2. Run 3-round convergence (rounds 0 & 1 request changes, round 2 approves -> human_gate)
rippflow run --project examples/multi-model-review/rippflow.yaml --input examples/multi-model-review/input.json --timeout-seconds 60 --json

# 3. Profile active launcher slices vs external callback wait times
rippflow profile --project examples/multi-model-review/rippflow.yaml --input examples/multi-model-review/input.json --timeout-seconds 60 --json

# 4. Deploy locally over HTTPS (exposing /v1/workflows/multi-model-review/run, /v1/callbacks, and /v1/runs)
rippflow deploy --local --project examples/multi-model-review/rippflow.yaml --json
```

## Exercising 10-Round `REWORK_LIMIT_EXHAUSTED`

Pass `{"forceExhaustion": true}` to verify bounded termination after 10 rounds (`loop_count == 0..=9`) with `human_gate` never invoked:

```sh
python3 -c 'import json, pathlib; pathlib.Path("/tmp/exhaust.json").write_text(json.dumps({"forceExhaustion": True}))'
rippflow run --project examples/multi-model-review/rippflow.yaml --input /tmp/exhaust.json --timeout-seconds 120 --json
```

Per-round verdict artifacts (`round-<N>.json` and `final-summary.json`) are written under `examples/multi-model-review/.outputs/multi-model-runs/<requestId>/` (or `$MULTI_MODEL_STATE_DIR/<requestId>/`).
