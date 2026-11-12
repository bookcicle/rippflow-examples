from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import threading
import time
import pytest

from pr_pipeline.callback_client import (
    build_callback_payload,
    redact_secrets,
    token_fingerprint,
    write_pending_escalation_envelope,
)
from pr_pipeline.models import PipelineConfig, _resolve_default_workspace, build_scoped_git_gh_env, load_dotenv_files
from pr_pipeline.notify import NotificationMessage, send_notification


REPO_ROOT = Path(__file__).resolve().parent.parent


def _find_rippflow_bin() -> str | None:
    candidates = [
        os.environ.get("RIPPFLOW_BIN"),
        str(Path.home() / ".cargo" / "target" / "debug" / "rippflow"),
        shutil.which("rippflow"),
    ]
    for cand in candidates:
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test Setup"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "setup@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "commit.gpgsign", "false"], check=True)
    (repo_dir / "README.md").write_text(f"# {repo_dir.name}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "initial commit"], check=True, capture_output=True)


class TestMultiOsNotifications:
    def test_macos_prefers_terminal_notifier_then_falls_back_to_osascript(self, tmp_path: Path) -> None:
        calls: list[list[str]] = []

        def fake_which_osascript_only(name: str) -> str | None:
            return "/usr/bin/osascript" if name == "osascript" else None

        def fake_runner(argv: list[str], timeout_sec: float = 5.0) -> tuple[bool, str]:
            calls.append(argv)
            return True, "delivered"

        msg = NotificationMessage(
            title='Rippflow "Alert"',
            subtitle="repo-a (Turn 15/15)",
            body="Needs human review",
            url="https://github.com/example/repo-a/pull/12",
        )
        log_file = tmp_path / "alerts.jsonl"
        receipt = send_notification(
            msg,
            platform_override="darwin",
            alert_log_path=log_file,
            which_fn=fake_which_osascript_only,
            runner=fake_runner,
        )
        assert receipt.delivered is True
        assert receipt.backend == "macos:osascript"
        assert len(calls) == 1
        assert calls[0][0] == "/usr/bin/osascript"
        assert "display notification" in calls[0][2]
        assert log_file.is_file()

    def test_linux_falls_back_from_notify_send_to_gdbus(self) -> None:
        calls: list[list[str]] = []

        def fake_which(name: str) -> str | None:
            if name == "notify-send":
                return "/usr/bin/notify-send"
            if name == "gdbus":
                return "/usr/bin/gdbus"
            return None

        def fake_runner(argv: list[str], timeout_sec: float = 5.0) -> tuple[bool, str]:
            calls.append(argv)
            if argv[0].endswith("notify-send"):
                return False, "DBus unavailable"
            return True, "(uint32 1,)"

        msg = NotificationMessage(
            title="Rippflow Escalation",
            subtitle="repo-linux (Turn 15/15)",
            body="Unresolved review comments",
        )
        receipt = send_notification(
            msg,
            platform_override="linux",
            which_fn=fake_which,
            runner=fake_runner,
        )
        assert receipt.delivered is True
        assert receipt.backend == "linux:gdbus"
        assert len(calls) == 2

    def test_windows_toast_and_balloon_fallback_escape_xml_and_quotes(self) -> None:
        calls: list[list[str]] = []

        def fake_which(name: str) -> str | None:
            return "C:\\Program Files\\PowerShell\\7\\pwsh.exe" if name == "pwsh" else None

        def fake_runner(argv: list[str], timeout_sec: float = 5.0) -> tuple[bool, str]:
            calls.append(argv)
            # First call (WinRT toast) fails, second call (WinForms balloon) succeeds
            if len(calls) == 1:
                return False, "Toast disabled"
            return True, "shown"

        msg = NotificationMessage(
            title="Repo <A> & 'B'",
            subtitle="Turn 15/15",
            body="Check O'Reilly's <xml> & review",
            url="https://github.com/example/repo/pull/9",
        )
        receipt = send_notification(
            msg,
            platform_override="win32",
            which_fn=fake_which,
            runner=fake_runner,
        )
        assert receipt.delivered is True
        assert receipt.backend == "windows:balloon"
        assert len(calls) == 2
        assert "&lt;A&gt; &amp;" in calls[0][-1]
        assert "O''Reilly''s" in calls[1][-1]


class TestIdentityAndCallbackSecurity:
    def test_scoped_git_gh_env_isolates_credentials_and_scrubs_signing_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "gho_leaked_ambient_token_12345")
        monkeypatch.setenv("RIPPFLOW_CALLBACK_SIGNING_KEY", "super-secret-key")
        env = build_scoped_git_gh_env(gh_user="author-bot", gh_token="gho_scoped_author_999")
        assert env["GH_TOKEN"] == "gho_scoped_author_999"
        assert env["GIT_AUTHOR_NAME"] == "author-bot"
        assert "GITHUB_TOKEN" not in env
        assert "RIPPFLOW_CALLBACK_SIGNING_KEY" not in env

    def test_secret_redaction_and_owner_only_escalation_envelope(self, tmp_path: Path) -> None:
        raw = "error with rfacb_v1_abcdef123456 and gho_secretgithubtoken999"
        redacted = redact_secrets(raw)
        assert "rfacb_v1_" not in redacted
        assert "gho_" not in redacted
        assert token_fingerprint("rfacb_v1_abcdef123456").startswith("sha256:")

        env_file = tmp_path / "escalations" / "repo-a.callback.json"
        req = {
            "tenantId": "local",
            "workflowId": "wf-1",
            "phaseId": "human_gate",
            "iterationId": "iter-0001",
            "operationId": "op-1",
            "dispatchGeneration": 1,
            "issuedOwnershipEpoch": 1,
            "issuedCellId": "cell-a",
            "callbackToken": "rfacb_v1_secret",
            "callbackUrl": "http://127.0.0.1:9999/callback",
        }
        write_pending_escalation_envelope(
            env_file,
            request_envelope=req,
            ca_cert_path=None,
            metadata={"shard": "repo-a"},
        )
        if os.name != "nt":
            mode = stat.S_IMODE(env_file.stat().st_mode)
            assert mode == 0o600
        built = build_callback_payload(req, status="COMPLETED", result_payload={"ok": True})
        assert built["status"] == "COMPLETED"
        assert built["operationId"] == "op-1"

    def test_distinct_identities_required_in_live_mode(self, tmp_path: Path) -> None:
        cfg = PipelineConfig.from_raw(
            {
                "featureRequest": "Add healthcheck endpoint",
                "workspaceDir": str(tmp_path),
                "authorGhUser": "same-user",
                "reviewerGhUser": "same-user",
                "executionMode": "live",
                "repos": ["svc-a"],
            },
            default_workspace=tmp_path,
        )
        assert cfg.author_gh_user == "same-user"
        from pr_pipeline.phases import handle_prepare_workspace

        with pytest.raises(ValueError, match="must be distinct GitHub accounts"):
            handle_prepare_workspace(
                {
                    "requestId": "req-same-id",
                    "payload": {
                        "featureRequest": "Add healthcheck endpoint",
                        "workspaceDir": str(tmp_path),
                        "authorGhUser": "same-user",
                        "reviewerGhUser": "same-user",
                        "executionMode": "live",
                        "repos": ["svc-a"],
                    },
                },
                REPO_ROOT,
            )


@pytest.mark.skipif(_find_rippflow_bin() is None, reason="rippflow CLI binary not installed on this runner")
class TestLiveRippflowEngineExecution:
    def test_parallel_multi_repo_rework_and_auto_approval(self, tmp_path: Path) -> None:
        rippflow_bin = _find_rippflow_bin()
        assert rippflow_bin is not None

        ws = tmp_path / "workspace"
        _init_git_repo(ws / "repo-alpha")
        _init_git_repo(ws / "repo-beta")

        input_path = tmp_path / "input.json"
        report_path = tmp_path / "report.json"
        input_path.write_text(
            json.dumps(
                {
                    "featureRequest": "Add compute_feature_value() returning 42 with unit tests",
                    "workspaceDir": str(ws),
                    "authorGhUser": "bot-author-account",
                    "reviewerGhUser": "lead-reviewer-account",
                    "executionMode": "dry_run",
                    "enableOsNotifications": False,
                    "maxTurns": 15,
                    "repos": ["repo-alpha", "repo-beta"],
                    "dryRunScenarios": {
                        # repo-alpha: Turn 1 fails local pytest -> Turn 2 passes pytest but reviewer leaves comment -> Turn 3 approved!
                        "repo-alpha": {"failTestsUntilTurn": 1, "requestChangesUntilTurn": 2},
                        # repo-beta: Turn 1 passes local pytest & approved immediately!
                        "repo-beta": {"failTestsUntilTurn": 0, "requestChangesUntilTurn": 0},
                    },
                }
            ),
            encoding="utf-8",
        )

        proc = subprocess.run(
            [
                rippflow_bin,
                "run",
                "workflow.yaml",
                "--handlers",
                "handlers.json",
                "--shards",
                "repo-alpha,repo-beta",
                "--input",
                str(input_path),
                "--report",
                str(report_path),
                "--timeout-seconds",
                "120",
                "--json",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["status"] == "completed"

        req_id = report["requestId"]
        summary_file = REPO_ROOT / ".runs" / req_id / "summary.json"
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
        by_repo = {r["repo"]: r for r in summary["repositories"]}

        assert by_repo["repo-alpha"]["turnsUsed"] == 3
        assert by_repo["repo-alpha"]["status"] == "approved"
        assert by_repo["repo-beta"]["turnsUsed"] == 1
        assert by_repo["repo-beta"]["status"] == "approved"

        # Verify external LLM & test phases suspended (`workerCapacityReleased: true`) and resumed via callback
        suspended_phases = {
            ev.get("phase")
            for ev in report.get("events", [])
            if ev.get("event") == "async_tool_suspended" and ev.get("workerCapacityReleased") is True
        }
        assert {"author_pr", "local_test_suite", "codex_review", "human_gate"}.issubset(suspended_phases)

        handlers_cfg = json.loads((REPO_ROOT / "handlers.json").read_text(encoding="utf-8"))
        for h_name, h_spec in handlers_cfg["handlers"].items():
            assert int(h_spec["timeoutSeconds"]) <= 60, f"handler {h_name} timeout must stay small (<=60s)"

        # Clean up run artifacts created during test
        subprocess.run(
            ["python3", "scripts/resolve_gate.py", "--cleanup-worktrees", "--run-id", req_id],
            cwd=str(REPO_ROOT),
            check=True,
        )
        shutil.rmtree(REPO_ROOT / ".runs" / req_id, ignore_errors=True)

    def test_fifteen_turn_cap_triggers_notification_suspends_and_resumes_on_human_callback(
        self, tmp_path: Path
    ) -> None:
        rippflow_bin = _find_rippflow_bin()
        assert rippflow_bin is not None

        ws = tmp_path / "workspace"
        shard_name = f"repo-escalate-{int(time.time() * 1000) % 100000}"
        _init_git_repo(ws / shard_name)

        input_path = tmp_path / "input-escalate.json"
        report_path = tmp_path / "report-escalate.json"
        input_path.write_text(
            json.dumps(
                {
                    "featureRequest": "Implement complex distributed consensus state machine",
                    "workspaceDir": str(ws),
                    "authorGhUser": "bot-author-account",
                    "reviewerGhUser": "lead-reviewer-account",
                    "executionMode": "dry_run",
                    "enableOsNotifications": False,
                    "maxTurns": 15,
                    "repos": [shard_name],
                    "dryRunScenarios": {
                        shard_name: {"alwaysComment": True},
                    },
                }
            ),
            encoding="utf-8",
        )

        resolved_holder: dict[str, str] = {}

        def human_approver_watcher() -> None:
            deadline = time.time() + 90.0
            runs_dir = REPO_ROOT / ".runs"
            while time.time() < deadline:
                matches = list(runs_dir.glob(f"*/escalations/{shard_name}.callback.json")) if runs_dir.exists() else []
                if matches:
                    res = subprocess.run(
                        [
                            "python3",
                            "scripts/resolve_gate.py",
                            "--shard",
                            shard_name,
                            "--decision",
                            "approve",
                            "--note",
                            "Human reviewed after 15 turns and approved.",
                        ],
                        cwd=str(REPO_ROOT),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    resolved_holder["stdout"] = res.stdout
                    resolved_holder["stderr"] = res.stderr
                    resolved_holder["rc"] = str(res.returncode)
                    return
                time.sleep(0.25)

        watcher = threading.Thread(target=human_approver_watcher, daemon=True)
        watcher.start()

        proc = subprocess.run(
            [
                rippflow_bin,
                "run",
                "workflow.yaml",
                "--handlers",
                "handlers.json",
                "--shards",
                shard_name,
                "--input",
                str(input_path),
                "--report",
                str(report_path),
                "--max-invocations",
                "200",
                "--timeout-seconds",
                "120",
                "--json",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        watcher.join(timeout=5.0)

        assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
        assert resolved_holder.get("rc") == "0", f"resolver failed: {resolved_holder}"

        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["status"] == "completed"
        assert any(
            ev.get("event") == "async_tool_suspended" and ev.get("workerCapacityReleased") is True
            for ev in report.get("events", [])
        )

        req_id = report["requestId"]
        notif_log = REPO_ROOT / ".runs" / req_id / "escalations" / "notifications.jsonl"
        assert notif_log.is_file()
        notif_entry = json.loads(notif_log.read_text(encoding="utf-8").splitlines()[0])
        assert "Turn 15/15" in notif_entry["subtitle"]

        summary = json.loads((REPO_ROOT / ".runs" / req_id / "summary.json").read_text(encoding="utf-8"))
        repo_summary = summary["repositories"][0]
        assert repo_summary["turnsUsed"] == 15
        assert repo_summary["status"] == "human_approved"
        assert repo_summary["humanResolution"]["decision"] == "approve"

        subprocess.run(
            ["python3", "scripts/resolve_gate.py", "--cleanup-worktrees", "--run-id", req_id],
            cwd=str(REPO_ROOT),
            check=True,
        )
        shutil.rmtree(REPO_ROOT / ".runs" / req_id, ignore_errors=True)


class TestDotenvAndNestedExamplesLayout:
    def test_dotenv_populates_identities_and_nested_workspace_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in (
            "WORKSPACE_DIR",
            "AUTHOR_GH_USER",
            "AUTHOR_GIT_EMAIL",
            "REVIEWER_GH_USER",
            "AUTHOR_MODEL",
            "REVIEWER_MODEL",
            "MAX_TURNS",
            "BRANCH_PREFIX",
        ):
            monkeypatch.delenv(key, raising=False)

        workspace = tmp_path / "workspace"
        examples_repo = workspace / "rippflow-examples"
        (examples_repo / ".git").mkdir(parents=True)
        wf_dir = examples_repo / "examples" / "pr-pipeline"
        wf_dir.mkdir(parents=True)

        assert _resolve_default_workspace(wf_dir) == workspace.resolve()

        env_file = wf_dir / ".env"
        env_file.write_text(
            f'WORKSPACE_DIR="{workspace}"\n'
            "AUTHOR_GH_USER=custom-author-bot\n"
            "AUTHOR_GIT_EMAIL=custom-author-bot@users.noreply.github.com\n"
            "REVIEWER_GH_USER=custom-reviewer\n"
            "MAX_TURNS=9\n"
            "BRANCH_PREFIX=ai/from-env\n",
            encoding="utf-8",
        )
        load_dotenv_files(wf_dir)
        cfg = PipelineConfig.from_raw(
            {"featureRequest": "Add metrics", "repos": ["svc-a"]},
            default_workspace=_resolve_default_workspace(wf_dir),
        )
        assert cfg.workspace_dir == str(workspace.resolve())
        assert cfg.author_gh_user == "custom-author-bot"
        assert cfg.author_git_email == "custom-author-bot@users.noreply.github.com"
        assert cfg.reviewer_gh_user == "custom-reviewer"
        assert cfg.max_turns == 9
        assert cfg.branch_prefix == "ai/from-env"
        assert cfg.repos["svc-a"].path == str((workspace / "svc-a").resolve())
