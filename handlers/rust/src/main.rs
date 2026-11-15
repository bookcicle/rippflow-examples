use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io::Read;

fn process_context(input: &str) -> Result<Value, String> {
    let context: Value =
        serde_json::from_str(input).map_err(|e| format!("invalid handler JSON: {e}"))?;

    let phase = context
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    let request_id = context
        .get("requestId")
        .and_then(Value::as_str)
        .unwrap_or("local-run");
    let shard = context
        .get("shard")
        .and_then(Value::as_str)
        .unwrap_or("default");
    let loop_count = context
        .get("loopCount")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let service = context
        .get("service")
        .and_then(Value::as_str)
        .unwrap_or("rust-example-handler");
    let options = context
        .get("options")
        .cloned()
        .unwrap_or_else(|| json!({}));

    if shard == "fatal-crash.bin" && phase == "process-item" {
        return Err(
            "SIMULATED_FATAL_CRASH: unrecoverable Rust worker fault for shard 'fatal-crash.bin'"
                .to_string(),
        );
    }

    let mut outcome = "completed";
    let mut reason: Option<&str> = None;

    if phase == "verify-balance" && shard == "stmt-feb.pdf" && loop_count == 0 {
        outcome = "rework";
        reason = Some("balance_mismatch_low_dpi");
    } else if phase == "loudness-qa" && shard == "scene-02.mp4" && loop_count == 0 {
        outcome = "rework";
        reason = Some("loudness_outside_target");
    } else if phase == "audit-item" && shard == "corrupt-item.bin" {
        outcome = "rework";
        reason = Some("persistent_checksum_corruption");
    } else if phase == "audit-item" && shard == "recoverable-item.bin" && loop_count == 0 {
        outcome = "rework";
        reason = Some("transient_checksum_drift");
    }

    let mut hasher = Sha256::new();
    hasher.update(format!("{request_id}:{phase}:{shard}:{loop_count}:{options}").as_bytes());
    let digest = hex::encode(hasher.finalize());

    Ok(json!({
        "outcome": outcome,
        "outputPointer": format!("s3://rippflow-examples/{request_id}/{phase}-{shard}-loop{loop_count}.json"),
        "payload": {
            "simulated": true,
            "runtime": "rust",
            "service": service,
            "phase": phase,
            "shard": shard,
            "loopCount": loop_count,
            "reason": reason,
            "sha256": digest,
            "optionsEcho": options
        }
    }))
}

fn main() {
    let mut input = String::new();
    if let Err(err) = std::io::stdin().read_to_string(&mut input) {
        eprintln!("failed to read stdin: {err}");
        std::process::exit(1);
    }
    match process_context(&input) {
        Ok(output) => {
            println!("{output}");
        }
        Err(err) => {
            eprintln!("{err}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::process_context;

    #[test]
    fn rework_then_complete_for_recoverable_item() {
        let first = process_context(
            r#"{"requestId":"r1","phase":"audit-item","shard":"recoverable-item.bin","loopCount":0,"service":"simulated.auditor","options":{}}"#,
        )
        .expect("loop 0 should succeed with rework");
        assert_eq!(first["outcome"], "rework");

        let second = process_context(
            r#"{"requestId":"r1","phase":"audit-item","shard":"recoverable-item.bin","loopCount":1,"service":"simulated.auditor","options":{}}"#,
        )
        .expect("loop 1 should complete");
        assert_eq!(second["outcome"], "completed");
    }

    #[test]
    fn corrupt_item_always_requests_rework() {
        let out = process_context(
            r#"{"requestId":"r1","phase":"audit-item","shard":"corrupt-item.bin","loopCount":2,"service":"simulated.auditor","options":{}}"#,
        )
        .expect("corrupt-item should return rework outcome");
        assert_eq!(out["outcome"], "rework");
    }

    #[test]
    fn fatal_crash_returns_err() {
        let err = process_context(
            r#"{"requestId":"r1","phase":"process-item","shard":"fatal-crash.bin","loopCount":0,"service":"simulated.processor","options":{}}"#,
        )
        .unwrap_err();
        assert!(err.contains("SIMULATED_FATAL_CRASH"));
    }

    #[test]
    fn statement_and_media_rework_at_loop_zero() {
        let stmt = process_context(
            r#"{"requestId":"r1","phase":"verify-balance","shard":"stmt-feb.pdf","loopCount":0}"#,
        )
        .unwrap();
        assert_eq!(stmt["outcome"], "rework");

        let media = process_context(
            r#"{"requestId":"r1","phase":"loudness-qa","shard":"scene-02.mp4","loopCount":0}"#,
        )
        .unwrap();
        assert_eq!(media["outcome"], "rework");
    }
}
