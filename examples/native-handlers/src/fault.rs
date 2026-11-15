//! Acceptance-only handler, never embedded in the three application examples.
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    path::PathBuf,
    time::Duration,
};
fn main() {
    let mut raw = String::new();
    std::io::stdin().read_to_string(&mut raw).unwrap();
    let input: Value = serde_json::from_str(&raw).unwrap();
    let phase = std::env::var("RIPPFLOW_PHASE").unwrap();
    let dir = PathBuf::from(std::env::var("RIPPFLOW_OUTPUT_DIR").unwrap());
    let mut log = std::fs::OpenOptions::new()
        .append(true)
        .create(true)
        .open(dir.join("attempts"))
        .unwrap();
    writeln!(log, "{phase}").unwrap();
    log.sync_all().unwrap();
    match std::fs::read_to_string(dir.join("behavior"))
        .unwrap_or_default()
        .as_str()
    {
        "invalid-output" => {
            println!("not JSON");
            return;
        }
        "timeout" => std::thread::sleep(Duration::from_secs(20)),
        "large-output" => {
            println!("{}", "x".repeat(17 * 1024 * 1024));
            return;
        }
        "memory" => {
            let _huge = vec![1u8; 1024 * 1024 * 1024];
            std::hint::black_box(_huge);
        }
        _ => {}
    }
    let pause = dir.join("pause");
    if std::fs::read_to_string(&pause).unwrap_or_default() == phase {
        let mut ready = std::fs::File::create(dir.join("ready")).unwrap();
        ready.write_all(phase.as_bytes()).unwrap();
        ready.sync_all().unwrap();
        while !dir.join("release").exists() {
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    let key = std::env::var("RIPPFLOW_IDEMPOTENCY_KEY").unwrap();
    let name = hex::encode(sha2::Sha256::digest(key.as_bytes()));
    let effect = dir.join(format!("effect-{phase}-{name}"));
    match std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&effect)
    {
        Ok(mut f) => {
            f.write_all(b"once").unwrap();
            f.sync_all().unwrap();
        }
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(e) => panic!("{e}"),
    }
    // Pause after the effect, before the handler's successful output/checkpoint.
    if std::fs::read_to_string(dir.join("after-effect")).unwrap_or_default() == phase
        && !dir.join("release").exists()
    {
        std::fs::write(dir.join("ready"), phase.as_bytes()).unwrap();
        while !dir.join("release").exists() {
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    println!("{}", json!({"outcome":"completed","payload":input}));
}
use sha2::Digest;
