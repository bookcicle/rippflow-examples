#!/usr/bin/env node
/**
 * JavaScript Workspace Helper Agent Plugin for Rippflow.
 * Transport: stdio
 * Protocol: rippflow-agent/v1
 *
 * Reads rippflow-agent/v1 JSON from stdin:
 *   {"protocol":"rippflow-agent/v1","action":"chat","agent":"js-workspace-helper","prompt":"...","model":"...","cwd":"..."}
 *
 * Emits NDJSON events to stdout:
 *   {"type":"thinking","text":"..."}
 *   {"type":"delta","text":"..."}
 *   {"type":"error","text":"..."}
 */

const fs = require('fs');
const path = require('path');
const readline = require('readline');

function emit(type, text) {
  process.stdout.write(JSON.stringify({ type, text }) + '\n');
}

function parsePackageJson(filePath) {
  try {
    const raw = fs.readFileSync(filePath, 'utf8');
    const pkg = JSON.parse(raw);
    return {
      type: 'Node.js / npm',
      file: 'package.json',
      name: pkg.name || 'unnamed',
      version: pkg.version || '0.0.0',
      description: pkg.description || '',
      scripts: pkg.scripts || {},
      dependencies: pkg.dependencies || {},
      devDependencies: pkg.devDependencies || {},
    };
  } catch (err) {
    return { type: 'Node.js / npm', file: 'package.json', error: err.message };
  }
}

function parseCargoToml(filePath) {
  try {
    const raw = fs.readFileSync(filePath, 'utf8');
    const lines = raw.split('\n');
    let inPackage = false;
    let inDeps = false;
    let name = '';
    let version = '';
    let edition = '';
    const deps = {};

    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith('#') || !trimmed) continue;
      if (trimmed.startsWith('[')) {
        inPackage = trimmed === '[package]';
        inDeps = trimmed === '[dependencies]';
        continue;
      }
      if (inPackage) {
        const mName = trimmed.match(/^name\s*=\s*["']([^"']+)["']/);
        if (mName) name = mName[1];
        const mVer = trimmed.match(/^version\s*=\s*["']([^"']+)["']/);
        if (mVer) version = mVer[1];
        const mEd = trimmed.match(/^edition\s*=\s*["']([^"']+)["']/);
        if (mEd) edition = mEd[1];
      } else if (inDeps) {
        const mDep = trimmed.match(/^([a-zA-Z0-9_\-]+)\s*=\s*(.*)$/);
        if (mDep) {
          deps[mDep[1]] = mDep[2].replace(/^["']|["']$/g, '');
        }
      }
    }
    return {
      type: 'Rust / Cargo',
      file: 'Cargo.toml',
      name: name || 'unnamed-crate',
      version: version || '0.1.0',
      edition: edition || '2021',
      dependencies: deps,
      scripts: {},
    };
  } catch (err) {
    return { type: 'Rust / Cargo', file: 'Cargo.toml', error: err.message };
  }
}

function parseDepSpec(raw) {
  const clean = raw.trim().replace(/^["']|["']$/g, '').trim();
  if (!clean) return null;

  // Split environment markers if present (e.g. "importlib-metadata>=0.12; python_version < '3.8'")
  let specPart = clean;
  let markerPart = '';
  const semiIdx = clean.indexOf(';');
  if (semiIdx !== -1) {
    specPart = clean.substring(0, semiIdx).trim();
    markerPart = clean.substring(semiIdx + 1).trim();
  }

  // Match package name, optional extras like [socks,security], and version specifier
  const m = specPart.match(/^([a-zA-Z0-9_\-\.]+)(\[[^\]]*\])?\s*(.*)$/);
  if (m) {
    const pkgName = m[1] + (m[2] || '');
    let versionSpec = (m[3] || '').trim();
    if (!versionSpec && markerPart) {
      versionSpec = `* (${markerPart})`;
    } else if (versionSpec && markerPart) {
      versionSpec = `${versionSpec} (${markerPart})`;
    }
    return { name: pkgName, version: versionSpec || '*' };
  }
  return { name: clean, version: '*' };
}

function parseTomlArray(text, startIndex) {
  // startIndex is the index of the opening '[' in text
  const items = [];
  let inString = false;
  let stringQuote = null;
  let currentString = '';
  let inComment = false;
  let depth = 0;
  let i = startIndex;

  while (i < text.length) {
    const ch = text[i];

    if (inComment) {
      if (ch === '\n') {
        inComment = false;
      }
      i++;
      continue;
    }

    if (inString) {
      if (ch === '\\' && stringQuote === '"') {
        i++;
        if (i < text.length) {
          currentString += text[i];
        }
      } else if (ch === stringQuote) {
        items.push(currentString);
        currentString = '';
        inString = false;
        stringQuote = null;
      } else {
        currentString += ch;
      }
      i++;
      continue;
    }

    if (ch === '#') {
      inComment = true;
      i++;
      continue;
    }

    if (ch === '"' || ch === "'") {
      inString = true;
      stringQuote = ch;
      currentString = '';
      i++;
      continue;
    }

    if (ch === '[') {
      depth++;
    } else if (ch === ']') {
      depth--;
      if (depth === 0) {
        i++;
        break;
      }
    }

    i++;
  }

  return { items, charsConsumed: i - startIndex, nextIndex: i };
}

function parsePyprojectToml(filePath) {
  try {
    const raw = fs.readFileSync(filePath, 'utf8');
    const lines = raw.split('\n');
    let currentSection = '';
    let name = '';
    let version = '';
    let description = '';
    const deps = {};
    const devDeps = {};
    const scripts = {};

    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      const trimmed = line.trim();

      if (!trimmed || trimmed.startsWith('#')) {
        i++;
        continue;
      }

      if (trimmed.startsWith('[')) {
        currentSection = trimmed.replace(/\s*#.*$/, '').trim();
        i++;
        continue;
      }

      if (currentSection === '[project]' || currentSection === '[tool.poetry]') {
        const mName = trimmed.match(/^name\s*=\s*["']([^"']+)["']/);
        if (mName) name = mName[1];
        const mVer = trimmed.match(/^version\s*=\s*["']([^"']+)["']/);
        if (mVer) version = mVer[1];
        const mDesc = trimmed.match(/^description\s*=\s*["']([^"']+)["']/);
        if (mDesc) description = mDesc[1];

        if (/^dependencies\s*=\s*\[/.test(trimmed)) {
          const remainingText = lines.slice(i).join('\n');
          const bracketIdx = remainingText.indexOf('[');
          const { items, nextIndex } = parseTomlArray(remainingText, bracketIdx);
          for (const item of items) {
            const parsed = parseDepSpec(item);
            if (parsed) deps[parsed.name] = parsed.version;
          }
          const consumedText = remainingText.substring(0, nextIndex);
          const newlinesConsumed = (consumedText.match(/\n/g) || []).length;
          i += newlinesConsumed + 1;
          continue;
        }
      } else if (currentSection === '[project.scripts]' || currentSection === '[tool.poetry.scripts]') {
        const mScript = trimmed.match(/^([a-zA-Z0-9_\-\.]+)\s*=\s*["']([^"']+)["']/);
        if (mScript) {
          scripts[mScript[1]] = mScript[2];
        }
      } else if (currentSection.startsWith('[project.optional-dependencies') || currentSection.startsWith('[tool.poetry.group.')) {
        if (/^[a-zA-Z0-9_\-\.]+\s*=\s*\[/.test(trimmed)) {
          const remainingText = lines.slice(i).join('\n');
          const bracketIdx = remainingText.indexOf('[');
          const { items, nextIndex } = parseTomlArray(remainingText, bracketIdx);
          for (const item of items) {
            const parsed = parseDepSpec(item);
            if (parsed) devDeps[parsed.name] = parsed.version;
          }
          const consumedText = remainingText.substring(0, nextIndex);
          const newlinesConsumed = (consumedText.match(/\n/g) || []).length;
          i += newlinesConsumed + 1;
          continue;
        } else {
          const mDep = trimmed.match(/^([a-zA-Z0-9_\-\.]+)\s*=\s*(.*)$/);
          if (mDep) {
            let val = mDep[2].replace(/^["']|["']$/g, '');
            const mVer = val.match(/version\s*=\s*["']([^"']+)["']/);
            if (mVer) val = mVer[1];
            devDeps[mDep[1]] = val;
          }
        }
      } else if (currentSection === '[tool.poetry.dependencies]' || currentSection === '[project.dependencies]') {
        const mDep = trimmed.match(/^([a-zA-Z0-9_\-\.]+)\s*=\s*(.*)$/);
        if (mDep) {
          let val = mDep[2].replace(/^["']|["']$/g, '');
          const mVer = val.match(/version\s*=\s*["']([^"']+)["']/);
          if (mVer) val = mVer[1];
          deps[mDep[1]] = val;
        }
      }

      i++;
    }

    return {
      type: 'Python / pyproject',
      file: 'pyproject.toml',
      name: name || 'unnamed-pyproject',
      version: version || '0.1.0',
      description: description || '',
      dependencies: deps,
      devDependencies: devDeps,
      scripts: scripts,
    };
  } catch (err) {
    return { type: 'Python / pyproject', file: 'pyproject.toml', error: err.message };
  }
}

async function run() {
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    terminal: false,
  });

  let receivedLine = null;

  for await (const line of rl) {
    if (line.trim().length > 0) {
      receivedLine = line.trim();
      break;
    }
  }

  if (!receivedLine) {
    emit('error', 'Malformed input: expected rippflow-agent/v1 JSON object on stdin');
    return;
  }

  let req;
  try {
    req = JSON.parse(receivedLine);
  } catch (err) {
    emit('error', `Malformed input: invalid JSON (${err.message})`);
    return;
  }

  if (typeof req !== 'object' || req === null) {
    emit('error', 'Malformed input: expected JSON object');
    return;
  }

  if (req.protocol !== 'rippflow-agent/v1') {
    emit('error', `Unsupported protocol "${req.protocol}", expected "rippflow-agent/v1"`);
    return;
  }

  const cwd = path.resolve(req.cwd || process.cwd());
  const prompt = (req.prompt || '').trim();

  emit('thinking', `Inspecting workspace manifests in ${cwd}...`);

  if (!fs.existsSync(cwd)) {
    emit('error', `Target workspace directory does not exist: ${cwd}`);
    return;
  }

  emit('thinking', 'Checking for package.json, Cargo.toml, or pyproject.toml...');

  const manifests = [];
  const pkgPath = path.join(cwd, 'package.json');
  const cargoPath = path.join(cwd, 'Cargo.toml');
  const pyprojectPath = path.join(cwd, 'pyproject.toml');

  if (fs.existsSync(pkgPath)) {
    manifests.push(parsePackageJson(pkgPath));
  }
  if (fs.existsSync(cargoPath)) {
    manifests.push(parseCargoToml(cargoPath));
  }
  if (fs.existsSync(pyprojectPath)) {
    manifests.push(parsePyprojectToml(pyprojectPath));
  }

  emit('thinking', `Found ${manifests.length} manifest file(s); compiling metadata and dependency tables...`);

  const md = [];
  const dirName = path.basename(cwd) || cwd;
  md.push(`## Workspace Manifest Report: \`${dirName}\`\n`);
  md.push(`| Property | Value |`);
  md.push(`| :--- | :--- |`);
  md.push(`| **Directory** | \`${cwd}\` |`);
  md.push(`| **Detected Manifests** | ${manifests.length > 0 ? manifests.map(m => `\`${m.file}\``).join(', ') : 'None'} |`);
  md.push('');

  if (manifests.length === 0) {
    md.push('No recognized project manifests (`package.json`, `Cargo.toml`, `pyproject.toml`) found in this directory.\n');
  } else {
    for (const m of manifests) {
      if (m.error) {
        md.push(`### Manifest: \`${m.file}\` (${m.type})\n`);
        md.push(`*Failed to parse manifest: ${m.error}*\n`);
        continue;
      }

      md.push(`### Manifest: \`${m.file}\` (${m.type})\n`);
      md.push(`| Attribute | Value |`);
      md.push(`| :--- | :--- |`);
      md.push(`| **Package Name** | \`${m.name}\` |`);
      md.push(`| **Version** | \`${m.version}\` |`);
      if (m.edition) md.push(`| **Edition** | \`${m.edition}\` |`);
      if (m.description) md.push(`| **Description** | ${m.description} |`);
      md.push('');

      // Scripts table
      const scriptEntries = Object.entries(m.scripts || {});
      if (scriptEntries.length > 0) {
        md.push('#### Scripts / Tasks\n');
        md.push('| Script | Command |');
        md.push('| :--- | :--- |');
        for (const [name, cmd] of scriptEntries) {
          md.push(`| \`${name}\` | \`${String(cmd).replace(/\|/g, '\\|')}\` |`);
        }
        md.push('');
      }

      // Dependencies table
      const depEntries = Object.entries(m.dependencies || {});
      const devEntries = Object.entries(m.devDependencies || {});
      const totalDeps = depEntries.length + devEntries.length;

      if (totalDeps > 0) {
        md.push('#### Dependencies\n');
        md.push('| Package | Version / Spec | Kind |');
        md.push('| :--- | :--- | :--- |');
        for (const [dep, ver] of depEntries) {
          md.push(`| \`${String(dep).replace(/\|/g, '\\|')}\` | \`${String(ver).replace(/\|/g, '\\|')}\` | runtime |`);
        }
        for (const [dep, ver] of devEntries) {
          md.push(`| \`${String(dep).replace(/\|/g, '\\|')}\` | \`${String(ver).replace(/\|/g, '\\|')}\` | dev |`);
        }
        md.push('');
      }
    }
  }

  if (prompt) {
    md.push('### Response\n');
    md.push(`**Prompt**: *"${prompt}"*\n`);
    md.push(`Workspace inspection for \`${dirName}\` is complete. The tables above detail the parsed manifests, task scripts, and package dependencies.`);
    md.push('');
  }

  emit('delta', md.join('\n'));
}

run().catch(err => {
  emit('error', `Unexpected failure: ${err.message}`);
});
