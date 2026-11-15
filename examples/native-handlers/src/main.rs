use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
fn main() {
    if let Err(e) = execute() {
        eprintln!("{e}");
        std::process::exit(1);
    }
}
fn execute() -> Result<(), Box<dyn std::error::Error>> {
    let mut text = String::new();
    std::io::stdin().take(1_048_577).read_to_string(&mut text)?;
    if text.len() > 1_048_576 {
        return Err("input exceeds 1 MiB".into());
    }
    let input: Value = serde_json::from_str(&text)?;
    let phase = std::env::var("RIPPFLOW_PHASE")?;
    let payload = match phase.as_str() {
        "sensor" => {
            let value = input["temperature"]
                .as_f64()
                .ok_or("temperature must be numeric")?;
            json!({"temperature":value,"alert":value>30.0})
        }
        "alert" => {
            json!({"alert":input["alert"],"message":if input["alert"]==true {"Temperature threshold exceeded"}else{"Temperature normal"}})
        }
        "capture" => {
            json!({"frameId":input["frameId"],"brightness":input["brightness"],"objects":input["objects"]})
        }
        "brightness" => {
            json!({"dark":input["brightness"].as_f64().ok_or("brightness must be numeric")?<0.2})
        }
        "objects" => {
            json!({"objectCount":input["objects"].as_array().ok_or("objects must be an array")?.len()})
        }
        "decision" => {
            json!({"inspection":"simulated-metadata","accepted":input["inputs"]["brightness"]["dark"]==false && input["inputs"]["objects"]["objectCount"].as_u64().unwrap_or(0)>0,"checks":input["inputs"]})
        }
        "ingest" => json!({"text":input["text"].as_str().ok_or("text is required")?}),
        "validate" => {
            if input["text"].as_str().unwrap_or("").trim().is_empty() {
                return Err("empty document".into());
            }
            input
        }
        "transform" => {
            json!({"text":input["text"].as_str().ok_or("text is required")?.trim().to_uppercase()})
        }
        "artifact" => {
            let dir = std::path::PathBuf::from(
                std::env::var("RIPPFLOW_OUTPUT_DIR")
                    .map_err(|_| "provide --output-dir for document artifacts")?,
            );
            let key = std::env::var("RIPPFLOW_IDEMPOTENCY_KEY")?;
            let filename = format!("{}.txt", hex::encode(Sha256::digest(key)));
            let path = dir.join(filename);
            let bytes = input["text"].as_str().ok_or("text missing")?.as_bytes();
            // Stable effect key makes recovery safe after an effect but before its checkpoint.
            match std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&path)
            {
                Ok(mut file) => {
                    file.write_all(bytes)?;
                    file.sync_all()?;
                    std::fs::File::open(&dir)?.sync_all()?;
                }
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                    if std::fs::read(&path)? != bytes {
                        return Err("artifact identity conflict".into());
                    }
                }
                Err(e) => return Err(e.into()),
            }
            json!({"artifact":path,"bytes":bytes.len(),"sha256":hex::encode(Sha256::digest(bytes))})
        }
        _ => return Err(format!("unknown phase {phase}").into()),
    };
    println!("{}", json!({"outcome":"completed","payload":payload}));
    Ok(())
}
