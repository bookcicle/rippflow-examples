use std::collections::BTreeMap;
use std::fs;
use std::io::{self, Read, Write};
use std::path::Path;
use std::time::Instant;

struct FileStat {
    rel_path: String,
    language: &'static str,
    lines: usize,
    bytes: u64,
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
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

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
    name.starts_with('.')
        || matches!(
            name,
            "target" | "node_modules" | "venv" | "dist" | "build" | "__pycache__" | "Library"
        )
}

fn detect_language(name: &str) -> Option<&'static str> {
    let lower = name.to_ascii_lowercase();
    let ext = Path::new(&lower).extension().and_then(|e| e.to_str())?;
    Some(match ext {
        "rs" => "Rust",
        "py" => "Python",
        "ts" | "tsx" => "TypeScript",
        "js" | "jsx" => "JavaScript",
        "go" => "Go",
        "yaml" | "yml" => "YAML",
        "json" => "JSON",
        "toml" => "TOML",
        "md" => "Markdown",
        "sh" | "bash" | "zsh" => "Shell",
        "sql" => "SQL",
        "html" | "css" => "Web",
        _ => return None,
    })
}

fn walk_workspace(
    dir: &Path,
    prefix: &str,
    depth: usize,
    lang_counts: &mut BTreeMap<&'static str, usize>,
    files: &mut Vec<FileStat>,
) {
    if depth > 4 || files.len() >= 400 {
        return;
    }
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.filter_map(|e| e.ok()) {
        if files.len() >= 400 {
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
                walk_workspace(&path, &rel, depth + 1, lang_counts, files);
            }
            continue;
        }
        let Some(lang) = detect_language(&name) else {
            continue;
        };
        *lang_counts.entry(lang).or_insert(0) += 1;
        let lines = if meta.len() <= 512 * 1024 {
            fs::read_to_string(&path)
                .map(|c| c.lines().count())
                .unwrap_or(0)
        } else {
            0
        };
        files.push(FileStat {
            rel_path: rel,
            language: lang,
            lines,
            bytes: meta.len(),
        });
    }
}

fn render_dynamic_ui_json(
    host_cwd: &str,
    elapsed_ms: u128,
    lang_counts: &BTreeMap<&'static str, usize>,
    files: &mut [FileStat],
) -> String {
    let total_files = files.len();
    let total_lines: usize = files.iter().map(|f| f.lines).sum();
    let lang_count = lang_counts.len();

    let mut lang_vec: Vec<(&str, usize)> = lang_counts.iter().map(|(k, v)| (*k, *v)).collect();
    lang_vec.sort_by(|a, b| b.1.cmp(&a.1));
    lang_vec.truncate(10);

    let labels_json: Vec<String> = lang_vec
        .iter()
        .map(|(l, _)| format!("\"{}\"", json_escape(l)))
        .collect();
    let values_json: Vec<String> = lang_vec.iter().map(|(_, c)| format!("{c}")).collect();

    files.sort_by(|a, b| b.lines.cmp(&a.lines).then(b.bytes.cmp(&a.bytes)));
    let mut rows_json = Vec::new();
    for f in files.iter().take(50) {
        let kb = (f.bytes as f64) / 1024.0;
        rows_json.push(format!(
            "[\"{}\", \"{}\", \"{}\", \"{:.1}\"]",
            json_escape(&f.rel_path),
            json_escape(f.language),
            f.lines,
            kb
        ));
    }

    format!(
        r##"{{
  "id": "trace.dashboard",
  "title": "Live Workspace Telemetry (Dynamic Wasm)",
  "icon": "activity",
  "layout": {{
    "type": "Container",
    "padding": 16,
    "gap": 16,
    "children": [
      {{
        "type": "Row",
        "gap": 12,
        "children": [
          {{
            "type": "MetricCard",
            "label": "Source Files",
            "value": "{total_files}",
            "trend": "{}"
          }},
          {{
            "type": "MetricCard",
            "label": "Lines of Code",
            "value": "{total_lines}",
            "trend": "{lang_count} languages",
            "status": "success"
          }},
          {{
            "type": "MetricCard",
            "label": "Wasm Scan Latency",
            "value": "{elapsed_ms}ms",
            "trend": "In-Process WASI",
            "status": "success"
          }},
          {{
            "type": "MetricCard",
            "label": "Sandbox Mode",
            "value": "Read-Only",
            "trend": "Air-Gapped",
            "status": "success"
          }}
        ]
      }},
      {{
        "type": "BarChart",
        "title": "Files by Language",
        "labels": [{}],
        "values": [{}],
        "color": "#5ef0bd"
      }},
      {{
        "type": "Table",
        "title": "Largest Workspace Files",
        "virtualized": true,
        "sticky_header": true,
        "columns": ["File", "Language", "Lines", "Size (KB)"],
        "rows": [{}]
      }}
    ]
  }}
}}"##,
        json_escape(host_cwd),
        labels_json.join(", "),
        values_json.join(", "),
        rows_json.join(", ")
    )
}

fn main() {
    let start = Instant::now();
    let mut stdin_buf = String::new();
    let _ = io::stdin().read_to_string(&mut stdin_buf);

    let action = extract_json_str(&stdin_buf, "action").unwrap_or_else(|| "ask".to_string());
    let host_cwd = extract_json_str(&stdin_buf, "cwd").unwrap_or_else(|| ".".to_string());

    let mut lang_counts: BTreeMap<&'static str, usize> = BTreeMap::new();
    let mut files: Vec<FileStat> = Vec::new();
    walk_workspace(Path::new("."), "", 0, &mut lang_counts, &mut files);
    let elapsed_ms = start.elapsed().as_millis();

    let mut stdout = io::stdout().lock();
    if action == "view" {
        let ui_json = render_dynamic_ui_json(&host_cwd, elapsed_ms, &lang_counts, &mut files);
        let _ = writeln!(stdout, "{ui_json}");
    } else {
        let total_lines: usize = files.iter().map(|f| f.lines).sum();
        let thinking = format!(
            r#"{{"type":"thinking","text":"Scanned {} files ({} LOC) in {}ms inside WASI sandbox..."}}"#,
            files.len(),
            total_lines,
            elapsed_ms
        );
        let delta_md = format!(
            "## Live Workspace Telemetry (`wasm-trace-lens`)\n\n- **Workspace:** `{host_cwd}`\n- **Files:** `{}` across `{}` languages\n- **Lines of Code:** `{total_lines}`\n- **Wasm Scan Time:** `{elapsed_ms}ms`\n",
            files.len(),
            lang_counts.len()
        );
        let delta = format!(r#"{{"type":"delta","text":"{}"}}"#, json_escape(&delta_md));
        let _ = writeln!(stdout, "{thinking}");
        let _ = writeln!(stdout, "{delta}");
    }
    let _ = stdout.flush();
}
