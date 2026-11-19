#!/usr/bin/env python3
"""
Unit tests for Rippflow Example Agent Plugins.
Validates all manifests and runs agent.py and agent.js over stdin/stdout
with rippflow-agent/v1 payloads, asserting valid NDJSON thinking and delta events.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Optional pyyaml import with fallback simple YAML reader
try:
    import yaml
except ImportError:
    yaml = None


def parse_manifest_file(file_path: str) -> dict:
    """Parses a JSON or YAML manifest file."""
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    if file_path.endswith(".json"):
        return json.loads(content)
    if yaml is not None:
        return yaml.safe_load(content)

    # Lightweight YAML parser fallback for basic key-value / lists
    res: dict = {}
    current_key = None
    sub_key = None
    for line in content.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        trimmed = line.strip()

        if indent == 0 and ":" in trimmed:
            k, v = trimmed.split(":", 1)
            current_key = k.strip()
            sub_key = None
            val = v.strip()
            if val:
                res[current_key] = val
            else:
                res[current_key] = {}
        elif indent == 2 and current_key:
            if trimmed.startswith("- "):
                if not isinstance(res[current_key], list):
                    res[current_key] = []
                res[current_key].append(trimmed[2:].strip().strip('"').strip("'"))
            elif ":" in trimmed:
                k, v = trimmed.split(":", 1)
                sub_key = k.strip()
                val = v.strip().strip('"').strip("'")
                if not isinstance(res[current_key], dict):
                    res[current_key] = {}
                if val:
                    if val.lower() == "true":
                        val_parsed = True
                    elif val.lower() == "false":
                        val_parsed = False
                    else:
                        val_parsed = val
                    res[current_key][sub_key] = val_parsed
                else:
                    res[current_key][sub_key] = {}
        elif indent == 4 and current_key and sub_key and isinstance(res[current_key], dict):
            if trimmed.startswith("- "):
                if not isinstance(res[current_key].get(sub_key), list):
                    res[current_key][sub_key] = []
                res[current_key][sub_key].append(trimmed[2:].strip().strip('"').strip("'"))
            elif ":" in trimmed:
                k, v = trimmed.split(":", 1)
                if not isinstance(res[current_key].get(sub_key), dict):
                    res[current_key][sub_key] = {}
                res[current_key][sub_key][k.strip()] = v.strip().strip('"').strip("'")

    return res


class TestManifestValidation(unittest.TestCase):
    """Validates the structure and fields of all three plugin manifests."""

    @classmethod
    def setUpClass(cls):
        cls.plugins_dir = Path(__file__).resolve().parent

    def test_ollama_manifest(self):
        manifest_path = self.plugins_dir / "ollama" / "ollama.yaml"
        self.assertTrue(manifest_path.is_file(), f"Missing manifest: {manifest_path}")

        spec = parse_manifest_file(str(manifest_path))
        self.assertEqual(spec.get("id"), "ollama")
        self.assertEqual(spec.get("name"), "Ollama")
        self.assertEqual(spec.get("binary"), "ollama")
        self.assertEqual(spec.get("transport"), "cli")

        invoke = spec.get("invoke", {})
        self.assertEqual(invoke.get("args"), ["run", "{prompt}"])
        self.assertEqual(invoke.get("model_args"), ["{model}"])
        self.assertIn(invoke.get("model_before_prompt"), [False, "false"])

        models = spec.get("models", [])
        expected_models = ["llama3.2", "qwen2.5-coder", "deepseek-r1", "mistral"]
        for m in expected_models:
            self.assertIn(m, models)

        doctor = spec.get("doctor", {})
        self.assertTrue(doctor.get("display_name"))
        self.assertTrue(doctor.get("official_url"))
        self.assertTrue(doctor.get("install_command"))
        self.assertIn("macos", doctor.get("install_by_os", {}))

    def test_py_repo_analyst_manifest(self):
        manifest_path = self.plugins_dir / "py-repo-analyst" / "py-repo-analyst.yaml"
        self.assertTrue(manifest_path.is_file(), f"Missing manifest: {manifest_path}")

        spec = parse_manifest_file(str(manifest_path))
        self.assertEqual(spec.get("id"), "py-repo-analyst")
        self.assertEqual(spec.get("name"), "Python Repo Analyst")
        self.assertEqual(spec.get("binary"), "python3")
        self.assertEqual(spec.get("transport"), "stdio")
        self.assertEqual(spec.get("parser"), "json_lines")

        invoke = spec.get("invoke", {})
        args = invoke.get("args", [])
        self.assertTrue(any("agent.py" in str(arg) for arg in args))

        doctor = spec.get("doctor", {})
        self.assertTrue(doctor.get("display_name"))
        self.assertTrue(doctor.get("official_url"))

    def test_js_workspace_helper_manifest(self):
        manifest_path = self.plugins_dir / "js-workspace-helper" / "js-workspace-helper.json"
        self.assertTrue(manifest_path.is_file(), f"Missing manifest: {manifest_path}")

        with open(manifest_path, "r", encoding="utf-8") as f:
            spec = json.load(f)

        self.assertEqual(spec.get("id"), "js-workspace-helper")
        self.assertEqual(spec.get("binary"), "node")
        self.assertEqual(spec.get("transport"), "stdio")
        self.assertEqual(spec.get("parser"), "json_lines")

        invoke = spec.get("invoke", {})
        args = invoke.get("args", [])
        self.assertTrue(any("agent.js" in str(arg) for arg in args))

        doctor = spec.get("doctor", {})
        self.assertTrue(doctor.get("display_name"))
        self.assertTrue(doctor.get("install_command"))

    def test_ds_evals_manifest(self):
        manifest_path = self.plugins_dir / "ds-evals" / "ds-evals.yaml"
        self.assertTrue(manifest_path.is_file(), f"Missing manifest: {manifest_path}")

        spec = parse_manifest_file(str(manifest_path))
        self.assertEqual(spec.get("id"), "ds-evals")
        self.assertEqual(spec.get("name"), "Model Training & Evaluation Studio")
        self.assertEqual(spec.get("binary"), "python3")

        contributes = spec.get("contributes", {})
        tabs = contributes.get("tabs", [])
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0].get("id"), "ds-evals.training")
        self.assertEqual(tabs[0].get("title"), "Model Performance")
        self.assertEqual(tabs[0].get("icon"), "line-chart")

        view_file = self.plugins_dir / "ds-evals" / tabs[0].get("view")
        self.assertTrue(view_file.is_file(), f"Missing dashboard view: {view_file}")
        with open(view_file, "r", encoding="utf-8") as f:
            dash = json.load(f)
        self.assertEqual(dash.get("id"), "ds-evals.training")

        drawers = contributes.get("drawers", [])
        self.assertEqual(len(drawers), 1)
        self.assertEqual(drawers[0].get("id"), "ds-evals.checkpoints")
        self.assertEqual(drawers[0].get("type"), "tree")

    def test_pipeline_dag_manifest(self):
        manifest_path = self.plugins_dir / "pipeline-dag" / "pipeline-dag.yaml"
        self.assertTrue(manifest_path.is_file(), f"Missing manifest: {manifest_path}")

        spec = parse_manifest_file(str(manifest_path))
        self.assertEqual(spec.get("id"), "pipeline-dag")
        self.assertEqual(spec.get("name"), "Pipeline DAG Visualizer")

        contributes = spec.get("contributes", {})
        tabs = contributes.get("tabs", [])
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0].get("id"), "pipeline.dag")

        view_file = self.plugins_dir / "pipeline-dag" / tabs[0].get("view")
        self.assertTrue(view_file.is_file(), f"Missing dag view: {view_file}")
        with open(view_file, "r", encoding="utf-8") as f:
            dag = json.load(f)
        self.assertEqual(dag.get("id"), "pipeline.dag")

        drawers = contributes.get("drawers", [])
        self.assertEqual(len(drawers), 1)
        self.assertEqual(drawers[0].get("id"), "pipeline.runs")

    def test_cluster_pulse_manifest(self):
        manifest_path = self.plugins_dir / "cluster-pulse" / "cluster-pulse.yaml"
        self.assertTrue(manifest_path.is_file(), f"Missing manifest: {manifest_path}")

        spec = parse_manifest_file(str(manifest_path))
        self.assertEqual(spec.get("id"), "cluster-pulse")
        self.assertEqual(spec.get("name"), "Cluster & Hardware Telemetry")

        contributes = spec.get("contributes", {})
        tabs = contributes.get("tabs", [])
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0].get("id"), "cluster.pulse")

        view_file = self.plugins_dir / "cluster-pulse" / tabs[0].get("view")
        self.assertTrue(view_file.is_file(), f"Missing telemetry view: {view_file}")

        drawers = contributes.get("drawers", [])
        self.assertEqual(len(drawers), 1)
        self.assertEqual(drawers[0].get("id"), "cluster.nodes")

        icon_packs = contributes.get("iconPacks", contributes.get("icon_packs", []))
        self.assertEqual(len(icon_packs), 1)
        self.assertEqual(icon_packs[0].get("id"), "compute-icons")

        icons_dir = self.plugins_dir / "cluster-pulse" / icon_packs[0].get("path")
        self.assertTrue(icons_dir.is_dir(), f"Missing icons dir: {icons_dir}")
        self.assertTrue((icons_dir / "apple-silicon.svg").is_file())
        self.assertTrue((icons_dir / "nvidia.svg").is_file())
        self.assertTrue((icons_dir / "cloud-node.svg").is_file())

    def test_bubbly_and_multicolor_icon_packs(self):
        packs = [
            ("solar-icons", False),
            ("fluent-emoji-icons", True),
        ]
        for pack_id, expect_multicolor in packs:
            plugin_dir = self.plugins_dir / pack_id
            manifest_path = plugin_dir / f"{pack_id}.yaml"
            self.assertTrue(manifest_path.is_file(), f"Missing manifest: {manifest_path}")
            spec = parse_manifest_file(str(manifest_path))
            self.assertEqual(spec.get("id"), pack_id)
            self.assertEqual(spec.get("type"), "ui")

            contributes = spec.get("contributes", {})
            icon_packs = contributes.get("iconPacks", contributes.get("icon_packs", []))
            self.assertEqual(len(icon_packs), 1)
            self.assertEqual(icon_packs[0].get("id"), pack_id)
            if expect_multicolor:
                self.assertTrue(icon_packs[0].get("multicolor"), f"{pack_id} should set multicolor: true")

            icons_dir = plugin_dir / icon_packs[0].get("path")
            self.assertTrue(icons_dir.is_dir(), f"Missing icons dir: {icons_dir}")
            svg_files = list(icons_dir.glob("*.svg"))
            self.assertGreaterEqual(len(svg_files), 85, f"{pack_id} has too few SVGs: {len(svg_files)}")
            for key in ["settings.svg", "folder.svg", "folder-open.svg", "agents-icon.svg", "file-rs.svg", "file-py.svg"]:
                content = (icons_dir / key).read_text(encoding="utf-8")
                self.assertIn("<svg", content)
                self.assertIn("</svg>", content)
            self.assertTrue((plugin_dir / "LICENSE").is_file(), f"Missing LICENSE for {pack_id}")

    def test_material_icons_manifest_and_assets(self):
        plugin_dir = self.plugins_dir / "material-icons"
        manifest_path = plugin_dir / "material-icons.yaml"
        self.assertTrue(manifest_path.is_file(), f"Missing manifest: {manifest_path}")

        spec = parse_manifest_file(str(manifest_path))
        self.assertEqual(spec.get("id"), "material-icons")
        self.assertEqual(spec.get("name"), "Material Design Icons")
        self.assertEqual(spec.get("type"), "ui")

        contributes = spec.get("contributes", {})
        icon_packs = contributes.get("iconPacks", contributes.get("icon_packs", []))
        self.assertEqual(len(icon_packs), 1)
        self.assertEqual(icon_packs[0].get("id"), "material-icons")

        icons_dir = plugin_dir / icon_packs[0].get("path")
        self.assertTrue(icons_dir.is_dir(), f"Missing icons dir: {icons_dir}")

        # Check key icons exist and are valid SVGs
        expected_icons = [
            "settings.svg",
            "search.svg",
            "folder.svg",
            "folder-open.svg",
            "copy.svg",
            "delete.svg",
            "agents-icon.svg",
            "threads-icon.svg",
            "square-terminal.svg",
            "git-branch.svg",
            "incognito-glasses.svg",
            "secrets-vault.svg",
            "preview-sparkle.svg",
            "file.svg",
            "file-py.svg",
            "file-rs.svg",
            "file-js.svg",
            "file-json.svg",
            "file-yaml.svg",
        ]
        for icon in expected_icons:
            icon_file = icons_dir / icon
            self.assertTrue(icon_file.is_file(), f"Missing icon: {icon}")
            content = icon_file.read_text(encoding="utf-8")
            self.assertIn("<svg", content)
            self.assertIn("</svg>", content)
            self.assertIn('viewBox="0 0 24 24"', content)

        license_file = plugin_dir / "LICENSE"
        self.assertTrue(license_file.is_file(), f"Missing LICENSE file: {license_file}")
        self.assertIn("Apache License", license_file.read_text(encoding="utf-8"))


class TestPyRepoAnalystExecution(unittest.TestCase):
    """Tests plugins/py-repo-analyst/agent.py over stdin/stdout."""

    @classmethod
    def setUpClass(cls):
        cls.script_path = str(Path(__file__).resolve().parent / "py-repo-analyst" / "agent.py")

    def run_agent(self, payload: dict | str) -> tuple[int, list[dict], str]:
        input_data = payload if isinstance(payload, str) else json.dumps(payload) + "\n"
        proc = subprocess.run(
            [sys.executable, self.script_path],
            input=input_data,
            capture_output=True,
            text=True,
            timeout=10,
        )
        events = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
        return proc.returncode, events, proc.stderr

    def test_workspace_analysis_in_git_repo(self):
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["git", "init"], cwd=td, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=td, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=td, check=True)

            readme = Path(td) / "README.md"
            readme.write_text("# Test Repo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=td, check=True)
            subprocess.run(["git", "commit", "-m", "initial commit"], cwd=td, check=True)

            payload = {
                "protocol": "rippflow-agent/v1",
                "action": "chat",
                "agent": "py-repo-analyst",
                "prompt": "What is the status?",
                "cwd": td,
            }
            code, events, stderr = self.run_agent(payload)
            self.assertEqual(code, 0, f"agent.py exited non-zero: {stderr}")

            event_types = [e.get("type") for e in events]
            self.assertIn("thinking", event_types)
            self.assertIn("delta", event_types)

            deltas = [e.get("text", "") for e in events if e.get("type") == "delta"]
            full_delta = "\n".join(deltas)
            self.assertIn("Repository Analysis", full_delta)
            self.assertIn("initial commit", full_delta)
            self.assertIn("Working tree is clean", full_delta)

    def test_with_rippflow_yaml_and_modified_files(self):
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["git", "init"], cwd=td, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=td, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=td, check=True)

            rf_yaml = Path(td) / "rippflow.yaml"
            rf_yaml.write_text("version: 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "rippflow.yaml"], cwd=td, check=True)
            subprocess.run(["git", "commit", "-m", "add rippflow config"], cwd=td, check=True)

            untracked = Path(td) / "scratch.txt"
            untracked.write_text("pending edit\n", encoding="utf-8")

            payload = {
                "protocol": "rippflow-agent/v1",
                "action": "ask",
                "agent": "py-repo-analyst",
                "prompt": "Check rippflow configuration",
                "cwd": td,
            }
            code, events, stderr = self.run_agent(payload)
            self.assertEqual(code, 0)

            deltas = [e.get("text", "") for e in events if e.get("type") == "delta"]
            full_delta = "\n".join(deltas)
            self.assertIn("✓ Present", full_delta)
            self.assertIn("uncommitted modification", full_delta)

    def test_malformed_input(self):
        code, events, _ = self.run_agent("{malformed json")
        self.assertEqual(code, 0)
        error_events = [e for e in events if e.get("type") == "error"]
        self.assertTrue(len(error_events) >= 1)
        self.assertIn("Malformed", error_events[0].get("text", ""))

    def test_unsupported_protocol(self):
        payload = {
            "protocol": "rippflow-agent/v999",
            "prompt": "hello",
        }
        code, events, _ = self.run_agent(payload)
        self.assertEqual(code, 0)
        error_events = [e for e in events if e.get("type") == "error"]
        self.assertTrue(len(error_events) >= 1)
        self.assertIn("Unsupported protocol", error_events[0].get("text", ""))


class TestJsWorkspaceHelperExecution(unittest.TestCase):
    """Tests plugins/js-workspace-helper/agent.js over stdin/stdout."""

    @classmethod
    def setUpClass(cls):
        cls.script_path = str(Path(__file__).resolve().parent / "js-workspace-helper" / "agent.js")
        cls.env = os.environ.copy()
        # Enable ES modules flag for older node versions (e.g. node 12)
        cls.env["NODE_OPTIONS"] = "--experimental-modules"

    def run_agent(self, payload: dict | str) -> tuple[int, list[dict], str]:
        input_data = payload if isinstance(payload, str) else json.dumps(payload) + "\n"
        proc = subprocess.run(
            ["node", self.script_path],
            input=input_data,
            capture_output=True,
            text=True,
            timeout=10,
            env=self.env,
        )
        events = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
        return proc.returncode, events, proc.stderr

    def test_package_json_inspection(self):
        with tempfile.TemporaryDirectory() as td:
            pkg = {
                "name": "my-demo-app",
                "version": "1.2.3",
                "scripts": {
                    "build": "vite build",
                    "test": "jest",
                },
                "dependencies": {
                    "react": "^18.2.0",
                },
                "devDependencies": {
                    "typescript": "^5.0.0",
                },
            }
            with open(Path(td) / "package.json", "w", encoding="utf-8") as f:
                json.dump(pkg, f)

            payload = {
                "protocol": "rippflow-agent/v1",
                "action": "chat",
                "agent": "js-workspace-helper",
                "prompt": "List scripts and dependencies",
                "cwd": td,
            }
            code, events, stderr = self.run_agent(payload)
            self.assertEqual(code, 0, f"agent.js failed: {stderr}")

            event_types = [e.get("type") for e in events]
            self.assertIn("thinking", event_types)
            self.assertIn("delta", event_types)

            full_delta = "\n".join(e.get("text", "") for e in events if e.get("type") == "delta")
            self.assertIn("my-demo-app", full_delta)
            self.assertIn("1.2.3", full_delta)
            self.assertIn("vite build", full_delta)
            self.assertIn("react", full_delta)
            self.assertIn("typescript", full_delta)

    def test_cargo_toml_inspection(self):
        with tempfile.TemporaryDirectory() as td:
            cargo_content = """[package]
name = "sample-crate"
version = "0.4.1"
edition = "2021"

[dependencies]
serde = "1.0"
tokio = "1.28"
"""
            (Path(td) / "Cargo.toml").write_text(cargo_content, encoding="utf-8")

            payload = {
                "protocol": "rippflow-agent/v1",
                "action": "chat",
                "agent": "js-workspace-helper",
                "prompt": "Inspect cargo dependencies",
                "cwd": td,
            }
            code, events, stderr = self.run_agent(payload)
            self.assertEqual(code, 0, f"agent.js failed: {stderr}")

            full_delta = "\n".join(e.get("text", "") for e in events if e.get("type") == "delta")
            self.assertIn("sample-crate", full_delta)
            self.assertIn("0.4.1", full_delta)
            self.assertIn("serde", full_delta)
            self.assertIn("tokio", full_delta)

    def test_pyproject_toml_inspection(self):
        with tempfile.TemporaryDirectory() as td:
            pyproject_content = """[project]
name = "sample-pkg"
version = "0.2.0"
description = "A standard PEP 621 sample package"
dependencies = [
    "requests[socks]>=2.0",
    "urllib3>=1.26,<3",
    "fastapi>=0.100.0",
    "pydantic>=2.0",
]

[project.scripts]
sample-cli = "sample_pkg.cli:main"

[project.optional-dependencies]
dev = [
    "pytest>=7.0,<8.0",
]
async = [
    "aiohttp[speedups]>=3.8.0,<4.0.0",
]
"""
            (Path(td) / "pyproject.toml").write_text(pyproject_content, encoding="utf-8")

            payload = {
                "protocol": "rippflow-agent/v1",
                "action": "chat",
                "agent": "js-workspace-helper",
                "prompt": "Inspect python dependencies",
                "cwd": td,
            }
            code, events, stderr = self.run_agent(payload)
            self.assertEqual(code, 0, f"agent.js failed: {stderr}")

            full_delta = "\n".join(e.get("text", "") for e in events if e.get("type") == "delta")
            self.assertIn("sample-pkg", full_delta)
            self.assertIn("requests[socks]", full_delta)
            self.assertIn(">=2.0", full_delta)
            self.assertIn("urllib3", full_delta)
            self.assertIn(">=1.26,<3", full_delta)
            self.assertIn("fastapi", full_delta)
            self.assertIn("pydantic", full_delta)
            self.assertIn("sample-cli", full_delta)
            self.assertIn("pytest", full_delta)
            self.assertIn(">=7.0,<8.0", full_delta)
            self.assertIn("aiohttp[speedups]", full_delta)
            self.assertIn(">=3.8.0,<4.0.0", full_delta)

    def test_pyproject_toml_quote_aware_array_and_comments(self):
        """Validates that bracket delimiters and commas inside quotes are preserved,
        comments outside quotes are stripped, and single-line arrays work properly."""
        with tempfile.TemporaryDirectory() as td:
            pyproject_content = """[project]
name = "quote-test"
version = "1.0.0"
dependencies = [
    "pkg-a[extra1,extra2]>=1.0.0,<=2.0.0", # extra bracket and comma in version
    'pkg-b[all]~=3.2.0',
]

[project.optional-dependencies]
test = ["coverage[toml]>=6.0,<7.0", "mock>=4.0"]
"""
            (Path(td) / "pyproject.toml").write_text(pyproject_content, encoding="utf-8")

            payload = {
                "protocol": "rippflow-agent/v1",
                "action": "chat",
                "agent": "js-workspace-helper",
                "prompt": "Check dependencies",
                "cwd": td,
            }
            code, events, stderr = self.run_agent(payload)
            self.assertEqual(code, 0, f"agent.js failed: {stderr}")

            full_delta = "\n".join(e.get("text", "") for e in events if e.get("type") == "delta")
            self.assertIn("pkg-a[extra1,extra2]", full_delta)
            self.assertIn(">=1.0.0,<=2.0.0", full_delta)
            self.assertIn("pkg-b[all]", full_delta)
            self.assertIn("~=3.2.0", full_delta)
            self.assertIn("coverage[toml]", full_delta)
            self.assertIn(">=6.0,<7.0", full_delta)
            self.assertIn("mock", full_delta)

    def test_malformed_input(self):
        code, events, _ = self.run_agent("{broken json")
        self.assertEqual(code, 0)
        errors = [e for e in events if e.get("type") == "error"]
        self.assertTrue(len(errors) >= 1)
        self.assertIn("Malformed input", errors[0].get("text", ""))

    def test_unsupported_protocol(self):
        payload = {
            "protocol": "bad-protocol/v1",
            "prompt": "test",
        }
        code, events, _ = self.run_agent(payload)
        self.assertEqual(code, 0)
        errors = [e for e in events if e.get("type") == "error"]
        self.assertTrue(len(errors) >= 1)
        self.assertIn("Unsupported protocol", errors[0].get("text", ""))


class TestInstallScript(unittest.TestCase):
    """Tests plugins/install.sh behavior and path rewriting."""

    @classmethod
    def setUpClass(cls):
        cls.install_sh = str(Path(__file__).resolve().parent / "install.sh")

    def test_install_copies_and_rewrites_paths(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            fake_home = td_path / "fake_home"
            fake_home.mkdir()

            # Test using a relative destination path containing special characters like '&'
            # to verify path canonicalization and safe manifest string serialization.
            rel_dest_name = "test & plugins"
            env = os.environ.copy()
            env["HOME"] = str(fake_home)

            res = subprocess.run(
                ["bash", self.install_sh, rel_dest_name],
                cwd=str(td_path),
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(res.returncode, 0, f"install.sh failed: {res.stderr}")

            # Check manifests installed in target dir
            dest = (td_path / rel_dest_name).resolve()
            ollama_m = dest / "ollama.yaml"
            py_m = dest / "py-repo-analyst.yaml"
            js_m = dest / "js-workspace-helper.json"

            self.assertTrue(ollama_m.is_file(), "Missing installed ollama.yaml")
            self.assertTrue(py_m.is_file(), "Missing installed py-repo-analyst.yaml")
            self.assertTrue(js_m.is_file(), "Missing installed js-workspace-helper.json")

            # Check script files copied and executable
            py_script = dest / "py-repo-analyst" / "agent.py"
            js_script = dest / "js-workspace-helper" / "agent.js"
            self.assertTrue(py_script.is_file(), "Missing installed agent.py")
            self.assertTrue(js_script.is_file(), "Missing installed agent.js")
            self.assertTrue(os.access(py_script, os.X_OK), "agent.py must be executable")
            self.assertTrue(os.access(js_script, os.X_OK), "agent.js must be executable")

            # Verify path rewriting in py-repo-analyst.yaml
            py_spec = parse_manifest_file(str(py_m))
            py_args = py_spec.get("invoke", {}).get("args", [])
            self.assertIn(str(py_script), py_args)

            # Verify path rewriting in js-workspace-helper.json
            with open(js_m, "r", encoding="utf-8") as f:
                js_spec = json.load(f)
            js_args = js_spec.get("invoke", {}).get("args", [])
            self.assertIn(str(js_script), js_args)

            # Verify agents mirror directory in isolated fake_home
            agents_dir = fake_home / ".rippflow" / "agents"
            self.assertTrue(agents_dir.is_dir(), "Mirror directory ~/.rippflow/agents was not created")
            self.assertTrue((agents_dir / "ollama.yaml").is_file(), "Missing mirrored ollama.yaml")
            self.assertTrue((agents_dir / "py-repo-analyst.yaml").is_file(), "Missing mirrored py-repo-analyst.yaml")
            self.assertTrue((agents_dir / "js-workspace-helper.json").is_file(), "Missing mirrored js-workspace-helper.json")
            self.assertTrue((agents_dir / "wasm-sec-audit.yaml").is_file(), "Missing mirrored wasm-sec-audit.yaml")
            self.assertTrue((dest / "wasm-sec-audit" / "wasm-sec-audit.wasm").is_file(), "Missing installed wasm-sec-audit.wasm")
            self.assertTrue((dest / "wasm-sec-audit" / "static_overview.json").is_file(), "Missing installed static_overview.json")
            self.assertTrue((agents_dir / "wasm-trace-lens.yaml").is_file(), "Missing mirrored wasm-trace-lens.yaml")
            self.assertTrue((dest / "wasm-trace-lens" / "wasm-trace-lens.wasm").is_file(), "Missing installed wasm-trace-lens.wasm")
            self.assertTrue((agents_dir / "material-icons.yaml").is_file(), "Missing mirrored material-icons.yaml")
            self.assertTrue((dest / "material-icons" / "icons" / "settings.svg").is_file(), "Missing installed material-icons settings.svg")
            self.assertTrue((dest / "material-icons" / "LICENSE").is_file(), "Missing installed material-icons LICENSE")

    def test_wasm_sec_audit_manifest_and_binary(self):
        plugin_dir = Path(__file__).resolve().parent / "wasm-sec-audit"
        manifest_path = plugin_dir / "wasm-sec-audit.yaml"
        wasm_path = plugin_dir / "wasm-sec-audit.wasm"
        static_view = plugin_dir / "static_overview.json"
        self.assertTrue(manifest_path.is_file(), f"Missing manifest: {manifest_path}")
        self.assertTrue(wasm_path.is_file(), f"Missing wasm module: {wasm_path}")
        self.assertTrue(static_view.is_file(), f"Missing static view: {static_view}")

        with open(wasm_path, "rb") as f:
            magic = f.read(8)
        self.assertEqual(magic, b"\x00asm\x01\x00\x00\x00", "Invalid WebAssembly binary magic header")

        spec = parse_manifest_file(str(manifest_path))
        self.assertEqual(spec.get("id"), "wasm-sec-audit")
        self.assertEqual(spec.get("transport"), "wasm")
        self.assertEqual(spec.get("wasm", {}).get("module"), "wasm-sec-audit.wasm")

    def test_wasm_trace_lens_manifest_and_binary(self):
        plugin_dir = Path(__file__).resolve().parent / "wasm-trace-lens"
        manifest_path = plugin_dir / "wasm-trace-lens.yaml"
        wasm_path = plugin_dir / "wasm-trace-lens.wasm"
        self.assertTrue(manifest_path.is_file(), f"Missing manifest: {manifest_path}")
        self.assertTrue(wasm_path.is_file(), f"Missing wasm module: {wasm_path}")

        with open(wasm_path, "rb") as f:
            magic = f.read(8)
        self.assertEqual(magic, b"\x00asm\x01\x00\x00\x00", "Invalid WebAssembly binary magic header")

        spec = parse_manifest_file(str(manifest_path))
        self.assertEqual(spec.get("id"), "wasm-trace-lens")
        self.assertEqual(spec.get("transport"), "wasm")
        self.assertEqual(spec.get("wasm", {}).get("module"), "wasm-trace-lens.wasm")


class TestDocViewerFileHandler(unittest.TestCase):
    """End-to-end tests for the PDF / DOCX / PPTX filetype handler plugin."""

    @classmethod
    def setUpClass(cls):
        cls.plugin_dir = Path(__file__).resolve().parent / "doc-viewer"
        cls.manifest_path = cls.plugin_dir / "doc-viewer.yaml"
        cls.script_path = cls.plugin_dir / "doc_viewer.py"

    def _invoke(self, req: dict) -> dict:
        proc = subprocess.run(
            [sys.executable, str(self.script_path)],
            input=json.dumps(req) + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"doc_viewer.py failed: {proc.stderr}")
        return json.loads(proc.stdout)

    def test_doc_viewer_manifest(self):
        self.assertTrue(self.manifest_path.is_file())
        self.assertTrue(self.script_path.is_file())
        raw = self.manifest_path.read_text(encoding="utf-8")
        self.assertIn("fileHandlers:", raw)
        for ext in ("- pdf", "- docx", "- pptx"):
            self.assertIn(ext, raw)

    def test_docx_open_and_switch_modes(self):
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            docx_path = Path(tmp) / "sample.docx"
            doc_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body>"
                '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Quarterly Report</w:t></w:r></w:p>'
                "<w:p><w:r><w:t>Revenue grew 24% year over year.</w:t></w:r></w:p>"
                "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Quarter</w:t></w:r></w:p></w:tc>"
                "<w:tc><w:p><w:r><w:t>Revenue</w:t></w:r></w:p></w:tc></w:tr>"
                "<w:tr><w:tc><w:p><w:r><w:t>Q4</w:t></w:r></w:p></w:tc>"
                "<w:tc><w:p><w:r><w:t>$4.2M</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
                "</w:body></w:document>"
            )
            core_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                "<dc:title>Q4 Executive Summary</dc:title><dc:creator>Rippflow</dc:creator>"
                "</cp:coreProperties>"
            )
            with zipfile.ZipFile(docx_path, "w") as zf:
                zf.writestr("word/document.xml", doc_xml)
                zf.writestr("docProps/core.xml", core_xml)

            spec = self._invoke({"protocol": "rippflow-agent/v1", "action": "open_file", "path": str(docx_path)})
            self.assertEqual(spec["id"], "doc-viewer.docx")
            children = spec["layout"]["children"]
            # Visual Page View renders a Canvas (or falls back to Markdown on headless systems)
            visual_or_md = [c for c in children if c.get("type") in ("Canvas", "Markdown")]
            self.assertTrue(visual_or_md)

            # Switch to Text (Markdown) view via file_event
            spec_text = self._invoke({
                "protocol": "rippflow-agent/v1",
                "action": "file_event",
                "path": str(docx_path),
                "event": "set_mode",
                "payload": {"mode": "document"},
                "state": spec.get("state"),
            })
            md_nodes = [c for c in spec_text["layout"]["children"] if c.get("type") == "Markdown"]
            self.assertTrue(md_nodes)
            self.assertIn("# Quarterly Report", md_nodes[0]["content"])
            self.assertIn("Revenue grew 24%", md_nodes[0]["content"])

            # Switch to Tables view via file_event
            spec_tbl = self._invoke({
                "protocol": "rippflow-agent/v1",
                "action": "file_event",
                "path": str(docx_path),
                "event": "set_mode",
                "payload": {"mode": "tables"},
                "state": spec.get("state"),
            })
            tbl_nodes = [c for c in spec_tbl["layout"]["children"] if c.get("type") == "Table"]
            self.assertEqual(len(tbl_nodes), 1)
            self.assertEqual(tbl_nodes[0]["columns"], ["Quarter", "Revenue"])
            self.assertEqual(tbl_nodes[0]["rows"], [["Q4", "$4.2M"]])

    def test_pptx_open_and_paginate(self):
        import zipfile

        a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
        with tempfile.TemporaryDirectory() as tmp:
            pptx_path = Path(tmp) / "deck.pptx"
            slide1 = f'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="{a_ns}"><a:p><a:r><a:t>Architecture Overview</a:t></a:r></a:p><a:p><a:r><a:t>Native GPUI Studio</a:t></a:r></a:p></p:sld>'
            slide2 = f'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="{a_ns}"><a:p><a:r><a:t>Plugin File Handlers</a:t></a:r></a:p><a:p><a:r><a:t>PDF, DOCX, PPTX in-app</a:t></a:r></a:p></p:sld>'
            notes2 = f'<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="{a_ns}"><a:p><a:r><a:t>Demo live pagination here.</a:t></a:r></a:p></p:notes>'
            with zipfile.ZipFile(pptx_path, "w") as zf:
                zf.writestr("ppt/slides/slide1.xml", slide1)
                zf.writestr("ppt/slides/slide2.xml", slide2)
                zf.writestr("ppt/notesSlides/notesSlide2.xml", notes2)

            spec1 = self._invoke({"protocol": "rippflow-agent/v1", "action": "open_file", "path": str(pptx_path)})
            self.assertEqual(spec1["state"]["page"], 1)
            visual1 = [c for c in spec1["layout"]["children"] if c.get("type") in ("Canvas", "Markdown")]
            self.assertTrue(visual1)

            spec2 = self._invoke({
                "protocol": "rippflow-agent/v1",
                "action": "file_event",
                "path": str(pptx_path),
                "event": "page_change",
                "payload": {"page": 2},
                "state": spec1["state"],
            })
            self.assertEqual(spec2["state"]["page"], 2)
            md_nodes2 = [c for c in spec2["layout"]["children"] if c.get("type") == "Markdown"]
            self.assertTrue(any("Demo live pagination" in m["content"] for m in md_nodes2))

    def test_pdf_open_zoom_and_text_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "brief.pdf"
            minimal_pdf = (
                b"%PDF-1.7\n"
                b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
                b"2 0 obj << /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >> endobj\n"
                b"3 0 obj << /Type /Page /Parent 2 0 R /Contents (Hello from Rippflow PDF Handler) >> endobj\n"
                b"4 0 obj << /Type /Page /Parent 2 0 R /Contents (Second Page Content) >> endobj\n"
                b"%%EOF\n"
            )
            pdf_path.write_bytes(minimal_pdf)

            spec = self._invoke({"protocol": "rippflow-agent/v1", "action": "open_file", "path": str(pdf_path)})
            self.assertEqual(spec["id"], "doc-viewer.pdf")
            canvases = [c for c in spec["layout"]["children"] if c.get("type") == "Canvas"]
            self.assertEqual(len(canvases), 1)
            self.assertTrue(canvases[0].get("png_base64") or canvases[0].get("rgba_base64"))

            spec_zoom = self._invoke({
                "protocol": "rippflow-agent/v1",
                "action": "file_event",
                "path": str(pdf_path),
                "event": "zoom_delta",
                "payload": {"delta": 0.25},
                "state": spec["state"],
            })
            self.assertAlmostEqual(spec_zoom["state"]["zoom"], 1.25)

            spec_text = self._invoke({
                "protocol": "rippflow-agent/v1",
                "action": "file_event",
                "path": str(pdf_path),
                "event": "set_mode",
                "payload": {"mode": "text"},
                "state": spec_zoom["state"],
            })
            md_nodes = [c for c in spec_text["layout"]["children"] if c.get("type") == "Markdown"]
            self.assertEqual(len(md_nodes), 1)
            self.assertIn("Hello from Rippflow PDF Handler", md_nodes[0]["content"])

    def test_rejects_xml_entity_bomb_and_uses_stdio_drawer_for_local_ports(self):
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            evil_docx = Path(tmp) / "billion_laughs.docx"
            bomb_xml = (
                '<?xml version="1.0"?>'
                '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>&lol2;</w:t></w:r></w:p></w:body></w:document>"
            )
            with zipfile.ZipFile(evil_docx, "w") as zf:
                zf.writestr("word/document.xml", bomb_xml)

            proc = subprocess.run(
                [sys.executable, str(self.script_path)],
                input=json.dumps(
                    {
                        "protocol": "rippflow-agent/v1",
                        "action": "open_file",
                        "path": str(evil_docx),
                    }
                )
                + "\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Document rejected", proc.stderr)

            oversized_docx = Path(tmp) / "oversized_header.docx"
            with zipfile.ZipFile(oversized_docx, "w") as zf:
                zf.writestr("word/document.xml", "<w:document/>")
            # Forge uncompressed size in local + central directory headers (> 64 MiB)
            raw_zip = bytearray(oversized_docx.read_bytes())
            # Central directory uncompressed size is at offset +24 from PK\x01\x02
            cd_idx = raw_zip.rfind(b"PK\x01\x02")
            self.assertGreaterEqual(cd_idx, 0)
            raw_zip[cd_idx + 24 : cd_idx + 28] = (80 * 1024 * 1024).to_bytes(4, "little")
            oversized_docx.write_bytes(bytes(raw_zip))

            proc_zip = subprocess.run(
                [sys.executable, str(self.script_path)],
                input=json.dumps(
                    {
                        "protocol": "rippflow-agent/v1",
                        "action": "open_file",
                        "path": str(oversized_docx),
                    }
                )
                + "\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(proc_zip.returncode, 0)
            self.assertIn("Document rejected", proc_zip.stderr)

        local_ports_dir = Path(__file__).resolve().parent / "local-ports"
        lp_manifest = (local_ports_dir / "local-ports.yaml").read_text(encoding="utf-8")
        self.assertIn("transport: stdio", lp_manifest)
        self.assertNotIn("command:", lp_manifest)

        lp_proc = subprocess.run(
            [sys.executable, str(local_ports_dir / "local_ports.py")],
            input=json.dumps(
                {
                    "protocol": "rippflow-agent/v1",
                    "action": "drawer",
                    "drawer": "local-ports.drawer",
                }
            )
            + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(lp_proc.returncode, 0, lp_proc.stderr)
        drawer_data = json.loads(lp_proc.stdout)
        self.assertIsInstance(drawer_data.get("sections"), list)


if __name__ == "__main__":
    unittest.main()
