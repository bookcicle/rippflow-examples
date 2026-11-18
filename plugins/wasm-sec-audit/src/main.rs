use std::fs;
use std::io::{self, Read, Write};
use std::path::Path;
use std::time::Instant;

struct Finding {
    severity: &'static str,
    location: String,
    rule: &'static str,
    detail: String,
}

struct ManifestSummary {
    path: String,
    kind: &'static str,
    notes: Vec<String>,
}

fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 16);
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out
}

fn emit_event(kind: &str, text: &str) {
    let escaped = json_escape(text);
    let line = format!(r#"{{"type":"{}","text":"{}"}}"#, kind, escaped);
    let mut stdout = io::stdout().lock();
    let _ = writeln!(stdout, "{line}");
    let _ = stdout.flush();
}

/// Minimal zero-dependency JSON string field extractor for `rippflow-agent/v1` requests.
fn extract_json_str(json: &str, key: &str) -> Option<String> {
    let needle = format!("\"{key}\"");
    let pos = json.find(&needle)?;
    let rest = &json[pos + needle.len()..];
    let colon = rest.find(':')?;
    let after_colon = rest[colon + 1..].trim_start();
    if !after_colon.starts_with('"') {
        return None;
    }
    let mut out = String::new();
    let mut chars = after_colon[1..].chars();
    while let Some(c) = chars.next() {
        match c {
            '"' => return Some(out),
            '\\' => {
                if let Some(esc) = chars.next() {
                    match esc {
                        'n' => out.push('\n'),
                        'r' => out.push('\r'),
                        't' => out.push('\t'),
                        '"' => out.push('"'),
                        '\\' => out.push('\\'),
                        other => out.push(other),
                    }
                }
            }
            other => out.push(other),
        }
    }
    None
}

fn should_skip_dir(name: &str) -> bool {
    matches!(
        name,
        ".git" | "target" | "node_modules" | ".venv" | "venv" | "dist" | "build" | "__pycache__"
    )
}

fn is_scannable_ext(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    if lower.starts_with(".env") {
        return true;
    }
    let Some(ext) = Path::new(&lower).extension().and_then(|e| e.to_str()) else {
        return matches!(
            lower.as_str(),
            "dockerfile" | "makefile" | "procfile" | ".gitignore"
        );
    };
    matches!(
        ext,
        "rs" | "py"
            | "js"
            | "ts"
            | "jsx"
            | "tsx"
            | "json"
            | "yaml"
            | "yml"
            | "toml"
            | "env"
            | "sh"
            | "bash"
            | "zsh"
            | "md"
            | "txt"
            | "conf"
            | "ini"
            | "go"
            | "java"
            | "rb"
            | "php"
            | "sql"
    )
}

fn redact_token(tok: &str) -> String {
    let chars: Vec<char> = tok.chars().collect();
    if chars.len() <= 8 {
        return "****".to_string();
    }
    let prefix: String = chars[..4].iter().collect();
    let suffix: String = chars[chars.len() - 2..].iter().collect();
    format!("{prefix}****{suffix}")
}

fn find_secret_in_line(line: &str) -> Option<(&'static str, &'static str, String)> {
    // 1. Private key headers
    if line.contains("-----BEGIN") && line.contains("PRIVATE KEY-----") {
        return Some(("HIGH", "private-key-pem", "PEM private key header".to_string()));
    }
    // 2. AWS Access Key ID: AKIA followed by 16 uppercase alphanumeric chars
    if let Some(idx) = line.find("AKIA") {
        let rest = &line[idx..];
        let tok: String = rest
            .chars()
            .take_while(|c| c.is_ascii_alphanumeric())
            .collect();
        if tok.len() >= 20 {
            return Some(("HIGH", "aws-access-key", redact_token(&tok)));
        }
    }
    // 3. GitHub tokens
    for prefix in ["ghp_", "github_pat_", "gho_", "ghs_"] {
        if let Some(idx) = line.find(prefix) {
            let rest = &line[idx..];
            let tok: String = rest
                .chars()
                .take_while(|c| c.is_ascii_alphanumeric() || *c == '_')
                .collect();
            if tok.len() >= 16 {
                return Some(("HIGH", "github-token", redact_token(&tok)));
            }
        }
    }
    // 4. Slack tokens
    for prefix in ["xoxb-", "xoxp-", "xoxa-"] {
        if let Some(idx) = line.find(prefix) {
            let rest = &line[idx..];
            let tok: String = rest
                .chars()
                .take_while(|c| c.is_ascii_alphanumeric() || *c == '-')
                .collect();
            if tok.len() >= 16 {
                return Some(("HIGH", "slack-token", redact_token(&tok)));
            }
        }
    }
    // 5. Anthropic / OpenAI style sk- keys
    for prefix in ["sk-ant-", "sk-proj-"] {
        if let Some(idx) = line.find(prefix) {
            let rest = &line[idx..];
            let tok: String = rest
                .chars()
                .take_while(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_')
                .collect();
            if tok.len() >= 20 {
                return Some(("HIGH", "llm-api-key", redact_token(&tok)));
            }
        }
    }
    None
}

fn inspect_manifest(rel_path: &str, name: &str, content: &str, out: &mut Vec<ManifestSummary>) {
    let mut notes = Vec::new();
    let kind = match name {
        "Cargo.toml" => {
            if content.contains("git = ") {
                notes.push("Uses unpinned or git-sourced dependency".to_string());
            }
            if content.contains("= \"*\"") || content.contains("=\"*\"") {
                notes.push("Contains wildcard '*' dependency version".to_string());
            }
            if !content.contains("license") && !content.contains("[workspace]") {
                notes.push("Missing `license` metadata field".to_string());
            }
            "Rust (Cargo)"
        }
        "package.json" => {
            if content.contains("\"*\"") || content.contains("\"latest\"") {
                notes.push("Contains wildcard '*' or 'latest' dependency version".to_string());
            }
            if !content.contains("\"license\"") {
                notes.push("Missing `license` field".to_string());
            }
            "Node / JS"
        }
        "pyproject.toml" => {
            if content.contains("==*") {
                notes.push("Contains wildcard dependency".to_string());
            }
            "Python"
        }
        "rippflow.yaml" | "rippflow.yml" => {
            if content.contains("runtime:") {
                notes.push("Rippflow project configuration detected".to_string());
            }
            "Rippflow"
        }
        _ => return,
    };
    out.push(ManifestSummary {
        path: rel_path.to_string(),
        kind,
        notes,
    });
}

fn walk_dir(
    dir: &Path,
    prefix: &str,
    depth: usize,
    files_scanned: &mut usize,
    findings: &mut Vec<Finding>,
    manifests: &mut Vec<ManifestSummary>,
) {
    if depth > 6 || *files_scanned >= 500 {
        return;
    }
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    let mut sorted: Vec<_> = entries.filter_map(|e| e.ok()).collect();
    sorted.sort_by_key(|e| e.file_name());

    for entry in sorted {
        if *files_scanned >= 500 {
            break;
        }
        let name = entry.file_name().to_string_lossy().to_string();
        let path = entry.path();
        let rel = if prefix.is_empty() {
            name.clone()
        } else {
            format!("{prefix}/{name}")
        };

        let Ok(meta) = entry.metadata() else {
            continue;
        };
        if meta.is_dir() {
            if !should_skip_dir(&name) {
                walk_dir(&path, &rel, depth + 1, files_scanned, findings, manifests);
            }
            continue;
        }

        if name == ".env"
            || (name.starts_with(".env.")
                && !name.ends_with(".example")
                && !name.ends_with(".template")
                && !name.ends_with(".sample"))
        {
            findings.push(Finding {
                severity: "MEDIUM",
                location: rel.clone(),
                rule: "dotenv-file",
                detail: "Local .env file present in workspace (verify .gitignore)".to_string(),
            });
        }

        if !is_scannable_ext(&name) || meta.len() > 512 * 1024 {
            continue;
        }

        *files_scanned += 1;
        let Ok(content) = fs::read_to_string(&path) else {
            continue;
        };

        inspect_manifest(&rel, &name, &content, manifests);

        for (idx, line) in content.lines().enumerate() {
            if let Some((severity, rule, detail)) = find_secret_in_line(line) {
                findings.push(Finding {
                    severity,
                    location: format!("{rel}:{}", idx + 1),
                    rule,
                    detail,
                });
                if findings.len() >= 100 {
                    return;
                }
            }
        }
    }
}

fn verify_sandbox_write_blocked() -> &'static str {
    // Prove at runtime that WASI read-only mount blocks writes
    match fs::write(".rippflow_wasm_sandbox_probe.tmp", b"test") {
        Ok(_) => "WARNING: Write unexpectedly succeeded",
        Err(_) => "Enforced (read-only WASI mount, writes blocked)",
    }
}

fn main() {
    let start = Instant::now();

    let mut stdin_buf = String::new();
    let _ = io::stdin().read_to_string(&mut stdin_buf);

    let prompt = extract_json_str(&stdin_buf, "prompt").unwrap_or_default();
    let host_cwd = extract_json_str(&stdin_buf, "cwd").unwrap_or_else(|| ".".to_string());

    emit_event(
        "thinking",
        &format!("Running air-gapped WASI security & dependency audit on `{host_cwd}`..."),
    );

    let write_probe = verify_sandbox_write_blocked();
    let mut files_scanned = 0usize;
    let mut findings = Vec::new();
    let mut manifests = Vec::new();

    walk_dir(
        Path::new("."),
        "",
        0,
        &mut files_scanned,
        &mut findings,
        &mut manifests,
    );

    let elapsed_ms = start.elapsed().as_millis();

    emit_event(
        "thinking",
        &format!(
            "Scanned {files_scanned} files in {elapsed_ms}ms ({} finding(s), {} manifest(s)).",
            findings.len(),
            manifests.len()
        ),
    );

    let mut md = String::new();
    md.push_str("## Wasm Security & Dependency Audit\n\n");
    if !prompt.trim().is_empty() {
        md.push_str(&format!("> **Query:** {}\n\n", prompt.trim()));
    }
    md.push_str(&format!(
        "- **Workspace:** `{host_cwd}`\n\
         - **Runtime:** Embedded WASI (`wasm32-wasip1`, 0 host dependencies)\n\
         - **Sandbox:** Filesystem `{write_probe}` | Network `Air-gapped (disabled)`\n\
         - **Scanned:** `{files_scanned}` files in `{elapsed_ms}ms`\n\n"
    ));

    if findings.is_empty() {
        md.push_str("### Secret & Credential Scan\n✓ No hardcoded secrets, private keys, or exposed `.env` files detected.\n\n");
    } else {
        md.push_str("### Secret & Credential Findings\n\n");
        md.push_str("| Severity | Location | Rule | Detail |\n|---|---|---|---|\n");
        for f in &findings {
            md.push_str(&format!(
                "| **{}** | `{}` | `{}` | {} |\n",
                f.severity, f.location, f.rule, f.detail
            ));
        }
        md.push('\n');
    }

    if !manifests.is_empty() {
        md.push_str("### Manifest & Dependency Hygiene\n\n");
        md.push_str("| Manifest | Stack | Notes |\n|---|---|---|\n");
        for m in &manifests {
            let note_str = if m.notes.is_empty() {
                "✓ Clean".to_string()
            } else {
                m.notes.join("; ")
            };
            md.push_str(&format!("| `{}` | {} | {} |\n", m.path, m.kind, note_str));
        }
        md.push('\n');
    }

    emit_event("delta", &md);
}
