#!/usr/bin/env python3
"""
Unit tests for Rippflow Example Agent Plugins.
Validates all manifests and runs agent.py and agent.mjs over stdin/stdout
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
        self.assertIn(invoke.get("model_before_prompt"), [True, "true"])

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
        self.assertTrue(any("agent.mjs" in str(arg) for arg in args))

        doctor = spec.get("doctor", {})
        self.assertTrue(doctor.get("display_name"))
        self.assertTrue(doctor.get("install_command"))


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
    """Tests plugins/js-workspace-helper/agent.mjs over stdin/stdout."""

    @classmethod
    def setUpClass(cls):
        cls.script_path = str(Path(__file__).resolve().parent / "js-workspace-helper" / "agent.mjs")
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
            self.assertEqual(code, 0, f"agent.mjs failed: {stderr}")

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
            self.assertEqual(code, 0, f"agent.mjs failed: {stderr}")

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
            self.assertEqual(code, 0, f"agent.mjs failed: {stderr}")

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
            self.assertEqual(code, 0, f"agent.mjs failed: {stderr}")

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
            js_script = dest / "js-workspace-helper" / "agent.mjs"
            self.assertTrue(py_script.is_file(), "Missing installed agent.py")
            self.assertTrue(js_script.is_file(), "Missing installed agent.mjs")
            self.assertTrue(os.access(py_script, os.X_OK), "agent.py must be executable")
            self.assertTrue(os.access(js_script, os.X_OK), "agent.mjs must be executable")

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


if __name__ == "__main__":
    unittest.main()
