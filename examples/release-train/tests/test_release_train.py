"""Unit and live `rippflow run` integration tests for `examples/release-train` (`local` + `cloud`)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
from unittest.mock import patch

import pytest

from release_train.callback_client import deliver_cloud_lambda_callback
from release_train.changelog import build_deterministic_changelog, synthesize_changelog
from release_train.cli_entry import lambda_handler
from release_train.models import ReleaseConfig, detect_rippflow_env, is_cloud_env
from release_train.notify import ReleaseAlertMessage, dispatch_signoff_notifications


WORKFLOW_DIR = Path(__file__).resolve().parents[1]


def _init_git_repo(repo_dir: Path, files: dict[str, str], commits: list[tuple[str, dict[str, str]]], initial_tag: str | None = "v0.1.0") -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo_dir), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test Bot"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "bot@example.invalid"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "commit.gpgsign", "false"], check=True, capture_output=True)

    for rel, content in files.items():
        fpath = repo_dir / rel
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "chore: initial v0.1.0 baseline"], check=True, capture_output=True)
    if initial_tag:
        subprocess.run(["git", "-C", str(repo_dir), "tag", "-a", initial_tag, "-m", f"Release {initial_tag}"], check=True, capture_output=True)

    for msg, patch_files in commits:
        for rel, content in patch_files.items():
            fpath = repo_dir / rel
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", msg], check=True, capture_output=True)

    return subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _find_rippflow_bin() -> str | None:
    return shutil.which("rippflow") or (
        "/usr/local/bin/rippflow" if Path("/usr/local/bin/rippflow").is_file() else None
    )


def test_cloud_env_strictly_flags_off_ai_changelog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When `RIPPFLOW_ENV=cloud` (or `AWS_LAMBDA_FUNCTION_NAME` is set), AI changelog drafting via
    Codex/Gemini is strictly flagged OFF even if `enableAiChangelog: true` is passed in the payload!
    """
    monkeypatch.setenv("RIPPFLOW_ENV", "cloud")
    assert detect_rippflow_env() == "cloud"
    assert is_cloud_env() is True

    cfg = ReleaseConfig.from_raw(
        {
            "releaseVersion": "1.4.0",
            "workspaceDir": str(tmp_path),
            "enableAiChangelog": True,
            "changelogProvider": "gemini",
            "repos": ["svc-core"],
        },
        workflow_dir=WORKFLOW_DIR,
    )
    assert cfg.rippflow_env == "cloud"
    assert cfg.ai_changelog_enabled is False
    assert cfg.changelog_provider == "deterministic"

    # Even if a caller directly invokes `synthesize_changelog(..., ai_enabled=True)` while `rippflow_env='cloud'`,
    # neither Gemini nor Codex may ever be called!
    with (
        patch("release_train.changelog._try_codex_changelog", side_effect=AssertionError("Codex must NOT run in cloud!")),
        patch("release_train.changelog._try_gemini_changelog_async", side_effect=AssertionError("Gemini must NOT run in cloud!")),
    ):
        md, gen = synthesize_changelog(
            repo_name="svc-core",
            release_version="1.4.0",
            commits=[
                "a1b2c3d feat(engine): support durable 7-day execution limits",
                "e4f5a6b fix(auth): redact callback tokens across split log chunks",
            ],
            pinned_sha_updates={"rippflow-core": {"old": "1111111000", "new": "2222222999"}},
            worktree=tmp_path,
            rippflow_env="cloud",
            ai_enabled=True,
            provider="auto",
        )

    assert gen == "deterministic:cloud-safe"
    assert "## [1.4.0]" in md
    assert "### Features" in md
    assert "**engine**: support durable 7-day execution limits (`a1b2c3d`)" in md
    assert "### Fixes" in md
    assert "### Pinned Dependency SHAs" in md
    assert "`1111111` -> `2222222`" in md


def test_deterministic_changelog_handles_empty_or_unconventional_commits() -> None:
    md = build_deterministic_changelog(
        repo_name="svc-docs",
        release_version="0.3.1",
        commits=[],
        pinned_sha_updates={},
    )
    assert "## [0.3.1]" in md
    assert "Synchronized `svc-docs` release metadata for `v0.3.1`." in md


def test_notifications_skip_desktop_in_cloud_and_deliver_webhooks(tmp_path: Path) -> None:
    received_webhooks: list[tuple[str, dict[str, Any]]] = []

    class _WebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            received_webhooks.append((self.path, body))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), _WebhookHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        msg = ReleaseAlertMessage(
            title="Release Sign-Off Needed",
            subtitle="v0.5.0 (3 shards)",
            body="Approve release v0.5.0",
            release_version="0.5.0",
            run_id="run-cloud-123",
            shards=["svc-core", "svc-starter", "svc-docs"],
        )
        log_path = tmp_path / "notifications.jsonl"

        def _forbid_desktop_runner(*_a: Any, **_kw: Any) -> tuple[bool, str]:
            raise AssertionError("Desktop GUI command must never execute when RIPPFLOW_ENV=cloud!")

        receipts = dispatch_signoff_notifications(
            msg,
            rippflow_env="cloud",
            slack_webhook_url=f"http://127.0.0.1:{port}/slack",
            email_webhook_url=f"http://127.0.0.1:{port}/email",
            alert_log_path=log_path,
            runner=_forbid_desktop_runner,
        )
    finally:
        server.shutdown()

    channels = {r.channel: r.delivered for r in receipts}
    assert channels["desktop:skipped_in_cloud"] is False
    assert channels["webhook:slack"] is True
    assert channels["webhook:email"] is True
    assert len(received_webhooks) == 2
    assert received_webhooks[0][0] == "/slack"
    assert "v0.5.0" in received_webhooks[0][1]["text"]
    assert received_webhooks[1][0] == "/email"
    assert received_webhooks[1][1]["releaseVersion"] == "0.5.0"
    assert log_path.is_file()


def test_cloud_aws_lambda_callback_adapter_and_rejection_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Verifies `deliver_cloud_lambda_callback` and `lambda_handler` match deployed AWS Lambda callback ingress."""
    invocations: list[dict[str, Any]] = []

    class _FakeLambdaClient:
        def invoke(self, **kwargs: Any) -> dict[str, Any]:
            invocations.append(kwargs)
            return {
                "StatusCode": 200,
                "Payload": io.BytesIO(json.dumps({"status": "async_callback_processed"}).encode("utf-8")),
            }

    class _FakeBoto3:
        @staticmethod
        def client(service: str) -> _FakeLambdaClient:
            assert service == "lambda"
            return _FakeLambdaClient()

    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3)

    ack = deliver_cloud_lambda_callback(
        "rippflow-cell-callback-ingress",
        {"workflowId": "wf-1", "operationId": "op-1", "callbackToken": "rfacb_v1_secret"},
        status="COMPLETED",
        result_payload={"shard": "svc-core"},
    )
    assert ack == {"status": "callback_accepted"}
    sent = json.loads(invocations[0]["Payload"].decode("utf-8"))
    assert sent["status"] == "SUCCEEDED"
    assert sent["result"] == {"shard": "svc-core"}

    # Verify `lambda_handler` routes inline cloud phase invocations cleanly
    svc_core = tmp_path / "svc-core"
    _init_git_repo(
        svc_core,
        {"Cargo.toml": '[package]\nname = "svc-core"\nversion = "0.1.0"\n'},
        [("feat: cloud lambda handler test", {"src/lib.rs": "pub fn v() -> u32 { 1 }\n"})],
    )
    monkeypatch.setenv("RIPPFLOW_STATE_DIR", str(tmp_path / "state_runs"))
    out = lambda_handler(
        {
            "phase": "detect_drift",
            "requestId": "wf-lambda-inline-1",
            "payload": {
                "releaseVersion": "0.2.0",
                "workspaceDir": str(tmp_path),
                "repos": [{"name": "svc-core", "path": str(svc_core)}],
            },
        },
        workflow_dir=WORKFLOW_DIR,
    )
    assert out["outcome"] == "completed"
    assert out["payload"]["rippflowEnv"] == "cloud"
    assert out["payload"]["aiChangelogEnabled"] is False


def test_live_rippflow_run_cloud_env_end_to_end_with_human_signoff(tmp_path: Path) -> None:
    """Runs the real `rippflow` engine with `--rippflow-env cloud` across 3 multi-repo shards:
    - Proves zero Codex/Gemini requirement in cloud mode (`changelogGenerator == 'deterministic:cloud-safe'`)
    - Proves `release_signoff_gate` suspends execution (`workerCapacityReleased`) and fires Slack webhook
    - Approves via `scripts/resolve_release_gate.py --decision approve` and verifies ordered tag/publish & docs sync!
    """
    rippflow_bin = _find_rippflow_bin()
    if not rippflow_bin:
        pytest.skip("rippflow CLI binary not installed on PATH")

    ws = tmp_path / "workspace"
    core_sha = _init_git_repo(
        ws / "svc-core",
        {"Cargo.toml": '[package]\nname = "svc-core"\nversion = "0.1.0"\nedition = "2021"\n'},
        [
            ("feat(runtime): add cloud-safe release pipeline", {"src/lib.rs": "pub fn ok() -> bool { true }\n"}),
            ("fix(manifest): preserve deterministic checksum order", {"README.md": "# svc-core\n"}),
        ],
    )
    _init_git_repo(
        ws / "svc-starter",
        {
            "pyproject.toml": '[project]\nname = "svc-starter"\nversion = "0.1.0"\n',
            "PINNED_SOURCE_SHAS.json": json.dumps({"svc-core": "0000000000000000000000000000000000000000"}, indent=2) + "\n",
        },
        [("feat(starter): wire cloud callback ingress", {"starter.py": "READY = True\n"})],
    )
    _init_git_repo(
        ws / "svc-docs",
        {"package.json": json.dumps({"name": "svc-docs", "version": "0.1.0"}, indent=2) + "\n"},
        [("docs: add release train guide", {"OVERVIEW.md": "# Overview\n"})],
    )

    webhook_hits: list[dict[str, Any]] = []

    class _SlackStub(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length", "0"))
            webhook_hits.append(json.loads(self.rfile.read(n).decode("utf-8")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), _SlackStub)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    unique_ver = f"0.2.{int(time.time() * 1000) % 100000}"
    py = sys.executable
    input_payload = {
        "releaseVersion": unique_ver,
        "workspaceDir": str(ws),
        "enableAiChangelog": True,  # Intentionally true to verify `--rippflow-env cloud` overrides it OFF!
        "executionMode": "live",
        "autoApproveGate": False,
        "slackWebhookUrl": f"http://127.0.0.1:{port}/slack",
        "publishOrder": ["svc-core", "svc-starter", "svc-docs"],
        "repos": [
            {
                "name": "svc-core",
                "path": str(ws / "svc-core"),
                "versionFile": "Cargo.toml",
                "verifyCommand": [py, "-c", "print('svc-core verified')"],
                "benchmarkCommand": [py, "-c", "print('p99=1.4ms')"],
            },
            {
                "name": "svc-starter",
                "path": str(ws / "svc-starter"),
                "versionFile": "pyproject.toml",
                "pinnedShaFile": "PINNED_SOURCE_SHAS.json",
                "verifyCommand": [py, "-c", "print('svc-starter verified')"],
            },
            {
                "name": "svc-docs",
                "path": str(ws / "svc-docs"),
                "versionFile": "package.json",
                "docsSyncTarget": "RELEASE_NOTES.md",
                "verifyCommand": [py, "-c", "print('svc-docs verified')"],
            },
        ],
    }
    input_file = tmp_path / "cloud_input.json"
    report_file = tmp_path / "cloud_report.json"
    input_file.write_text(json.dumps(input_payload), encoding="utf-8")

    proc = subprocess.Popen(
        [
            rippflow_bin,
            "run",
            "workflow.yaml",
            "--handlers",
            "handlers.json",
            "--rippflow-env",
            "cloud",
            "--shards",
            "svc-core,svc-starter,svc-docs",
            "--input",
            str(input_file),
            "--report",
            str(report_file),
            "--timeout-seconds",
            "120",
            "--json",
        ],
        cwd=str(WORKFLOW_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    run_id: str | None = None
    signoff_file: Path | None = None
    try:
        runs_dir = WORKFLOW_DIR / ".runs"
        deadline = time.time() + 30.0
        while time.time() < deadline and signoff_file is None:
            if proc.poll() is not None:
                out, err = proc.communicate()
                pytest.fail(f"rippflow exited before reaching release_signoff_gate:\nSTDOUT:\n{out}\nSTDERR:\n{err}")
            for cand in (runs_dir.glob("*/signoff/release_signoff.callback.json") if runs_dir.exists() else []):
                try:
                    rec = json.loads(cand.read_text(encoding="utf-8"))
                    if (rec.get("metadata") or {}).get("releaseVersion") == unique_ver:
                        signoff_file = cand
                        run_id = str(rec["requestEnvelope"]["workflowId"])
                        break
                except Exception:
                    pass
            if signoff_file is None:
                time.sleep(0.2)

        assert signoff_file is not None and run_id is not None, "Timed out waiting for release_signoff_gate callback file"
        assert len(webhook_hits) == 1
        assert unique_ver in webhook_hits[0]["text"]

        # Verify `--list` shows the pending release gate
        list_res = subprocess.run(
            [py, "scripts/resolve_release_gate.py", "--list"],
            cwd=str(WORKFLOW_DIR),
            check=True,
            capture_output=True,
            text=True,
        )
        listed = json.loads(list_res.stdout)["pendingReleaseGates"]
        assert any(g["runId"] == run_id and g["rippflowEnv"] == "cloud" for g in listed)

        # Approve the human release sign-off gate via authenticated HMAC callback!
        approve_res = subprocess.run(
            [
                py,
                "scripts/resolve_release_gate.py",
                "--run-id",
                run_id,
                "--decision",
                "approve",
                "--approver",
                "ci-release-lead",
                "--note",
                "All 3 cloud shards verified",
            ],
            cwd=str(WORKFLOW_DIR),
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(approve_res.stdout)["ok"] is True

        stdout, stderr = proc.communicate(timeout=30)
        assert proc.returncode == 0, f"rippflow run failed:\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
        report = json.loads(report_file.read_text(encoding="utf-8"))
        assert report["status"] == "completed"
        suspended = {
            ev.get("phase")
            for ev in report.get("events", [])
            if ev.get("event") == "async_tool_suspended" and ev.get("workerCapacityReleased") is True
        }
        assert {"bump_and_changelog", "verify_build_and_checksums", "release_signoff_gate"}.issubset(suspended)

        manifest = json.loads((WORKFLOW_DIR / ".runs" / run_id / "release-manifest.json").read_text(encoding="utf-8"))
        assert manifest["rippflowEnv"] == "cloud"
        assert manifest["aiChangelogEnabled"] is False
        assert [s["shard"] for s in manifest["publishedShards"]] == ["svc-core", "svc-starter", "svc-docs"]
        assert all(s["changelogGenerator"] == "deterministic:cloud-safe" for s in manifest["publishedShards"])

        # Verify version bumps, pinned SHA updates, docs sync, and live git tags!
        starter_wt = WORKFLOW_DIR / ".runs" / run_id / "worktrees" / "svc-starter"
        pinned = json.loads((starter_wt / "PINNED_SOURCE_SHAS.json").read_text(encoding="utf-8"))
        assert pinned["svc-core"] == core_sha

        docs_wt = WORKFLOW_DIR / ".runs" / run_id / "worktrees" / "svc-docs"
        synced_notes = (docs_wt / "RELEASE_NOTES.md").read_text(encoding="utf-8")
        assert f"# Release Notes — v{unique_ver}" in synced_notes
        assert "svc-core" in synced_notes and "svc-starter" in synced_notes

        for rname in ("svc-core", "svc-starter", "svc-docs"):
            tags = subprocess.run(
                ["git", "-C", str(ws / rname), "tag", "-l", f"v{unique_ver}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert tags == f"v{unique_ver}"
    finally:
        server.shutdown()
        if proc.poll() is None:
            proc.kill()
        if run_id:
            subprocess.run(
                [py, "scripts/resolve_release_gate.py", "--cleanup-worktrees", "--run-id", run_id],
                cwd=str(WORKFLOW_DIR),
                check=False,
                capture_output=True,
            )
            shutil.rmtree(WORKFLOW_DIR / ".runs" / run_id, ignore_errors=True)


def test_live_rippflow_run_local_env_and_signoff_rejection(tmp_path: Path) -> None:
    """Verifies `--rippflow-env local` human sign-off rejection aborts the release BEFORE `publish_and_sync_docs` tags anything."""
    rippflow_bin = _find_rippflow_bin()
    if not rippflow_bin:
        pytest.skip("rippflow CLI binary not installed on PATH")

    ws = tmp_path / "workspace_reject"
    _init_git_repo(
        ws / "svc-alpha",
        {"pyproject.toml": '[project]\nname = "svc-alpha"\nversion = "1.0.0"\n'},
        [("feat: candidate change", {"alpha.py": "X = 2\n"})],
        initial_tag="v1.0.0",
    )

    reject_ver = f"1.1.{int(time.time() * 1000) % 100000}"
    py = sys.executable
    input_file = tmp_path / "reject_input.json"
    report_file = tmp_path / "reject_report.json"
    input_file.write_text(
        json.dumps(
            {
                "releaseVersion": reject_ver,
                "workspaceDir": str(ws),
                "enableAiChangelog": False,
                "executionMode": "live",
                "autoApproveGate": False,
                "repos": [
                    {
                        "name": "svc-alpha",
                        "path": str(ws / "svc-alpha"),
                        "verifyCommand": [py, "-c", "print('ok')"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [
            rippflow_bin,
            "run",
            "workflow.yaml",
            "--handlers",
            "handlers.json",
            "--rippflow-env",
            "local",
            "--shards",
            "svc-alpha",
            "--input",
            str(input_file),
            "--report",
            str(report_file),
            "--timeout-seconds",
            "120",
            "--json",
        ],
        cwd=str(WORKFLOW_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    run_id: str | None = None
    signoff_file: Path | None = None
    try:
        runs_dir = WORKFLOW_DIR / ".runs"
        deadline = time.time() + 25.0
        while time.time() < deadline and signoff_file is None:
            if proc.poll() is not None:
                out, err = proc.communicate()
                pytest.fail(f"rippflow exited prematurely:\nSTDOUT:\n{out}\nSTDERR:\n{err}")
            for cand in (runs_dir.glob("*/signoff/release_signoff.callback.json") if runs_dir.exists() else []):
                try:
                    rec = json.loads(cand.read_text(encoding="utf-8"))
                    if (rec.get("metadata") or {}).get("releaseVersion") == reject_ver:
                        signoff_file = cand
                        run_id = str(rec["requestEnvelope"]["workflowId"])
                        break
                except Exception:
                    pass
            if signoff_file is None:
                time.sleep(0.2)
        assert signoff_file is not None and run_id is not None

        subprocess.run(
            [
                py,
                "scripts/resolve_release_gate.py",
                "--run-id",
                run_id,
                "--decision",
                "reject",
                "--note",
                "Hold release for hotfix",
            ],
            cwd=str(WORKFLOW_DIR),
            check=True,
            capture_output=True,
            text=True,
        )

        stdout, _stderr = proc.communicate(timeout=20)
        assert proc.returncode != 0 or json.loads(stdout)["status"] == "failed"

        # Confirm tag was NEVER created on `svc-alpha`!
        tags = subprocess.run(
            ["git", "-C", str(ws / "svc-alpha"), "tag", "-l", f"v{reject_ver}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert tags == ""
    finally:
        if proc.poll() is None:
            proc.kill()
        if run_id:
            subprocess.run(
                [py, "scripts/resolve_release_gate.py", "--cleanup-worktrees", "--run-id", run_id],
                cwd=str(WORKFLOW_DIR),
                check=False,
                capture_output=True,
            )
            shutil.rmtree(WORKFLOW_DIR / ".runs" / run_id, ignore_errors=True)
