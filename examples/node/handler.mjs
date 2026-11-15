#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";

let raw = "";
try {
  raw = fs.readFileSync(0, "utf8");
} catch (err) {
  console.error(`failed to read stdin: ${err.message}`);
  process.exit(1);
}

let context;
try {
  context = JSON.parse(raw);
} catch (err) {
  console.error(`invalid handler JSON: ${err.message}`);
  process.exit(1);
}

const phase = String(context.phase ?? "unknown");
const requestId = String(context.requestId ?? "local-run");
const shard = String(context.shard ?? "default");
const service = String(context.service ?? "node-example-handler");
const loopCount = Number(context.loopCount ?? 0);
const options =
  context.options && typeof context.options === "object"
    ? context.options
    : {};

if (shard === "fatal-crash.bin" && phase === "process-item") {
  console.error(
    "SIMULATED_FATAL_CRASH: unrecoverable Node.js worker fault for shard 'fatal-crash.bin'",
  );
  process.exit(1);
}

let outcome = "completed";
let reason = null;

if (phase === "verify-balance" && shard === "stmt-feb.pdf" && loopCount === 0) {
  outcome = "rework";
  reason = "balance_mismatch_low_dpi";
} else if (phase === "loudness-qa" && shard === "scene-02.mp4" && loopCount === 0) {
  outcome = "rework";
  reason = "loudness_outside_target";
} else if (phase === "audit-item" && shard === "corrupt-item.bin") {
  outcome = "rework";
  reason = "persistent_checksum_corruption";
} else if (
  phase === "audit-item" &&
  shard === "recoverable-item.bin" &&
  loopCount === 0
) {
  outcome = "rework";
  reason = "transient_checksum_drift";
}

const digest = crypto
  .createHash("sha256")
  .update(
    `${requestId}:${phase}:${shard}:${loopCount}:${JSON.stringify(options)}`,
  )
  .digest("hex");

console.log(
  JSON.stringify({
    outcome,
    outputPointer: `s3://rippflow-examples/${requestId}/${phase}-${shard}-loop${loopCount}.json`,
    payload: {
      simulated: true,
      runtime: "node",
      service,
      phase,
      shard,
      loopCount,
      reason,
      sha256: digest,
      optionsEcho: options,
    },
  }),
);
