"""Unit and live `rippflow run` integration tests for `examples/nightwatch` (`local` + `cloud`)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
from unittest.mock import patch

import pytest

from nightwatch.auditor import detect_flaky_tests
from nightwatch.cli_entry import lambda_handler
from nightwatch.hygiene import (
    clean_stale_repo_worktrees,
    reclaim_cargo_targets,
    reclaim_docker_images,
)
from nightwatch.models import NightwatchConfig, NightwatchRepoSpec, detect_rippflow_env
from nightwatch.upgrader import upgrade_and_adapt_worktree


WORKFLOW_DIR = Path(__file__).resolve().parents[1]


def _init_git_repo(repo_dir: Path, files: dict[str, str]) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo_dir), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test Bot"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "bot@example.invalid"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "commit.gpgsign", "false"], check=True, capture_output=True)
    for rel, content in files.items():
        fp = repo_dir / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "chore: initial baseline"], check=True, capture_output=True)
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


def test_hygiene_reclaims_stale_worktrees_preserves_dirty_ones_and_deletes_cargo_target(tmp_path: Path) -> None:
    """Verifies:
    1. Clean linked git worktrees older than `stale_hours` are removed in `live` mode.
    2. Dirty linked git worktrees (uncommitted changes) are NEVER deleted!
    3. Primary repo checkout is NEVER deleted!
    4. Rust `target/` directory is deleted and bytes reclaimed are counted accurately.
    """
    repo_dir = tmp_path / "svc-rust"
    _init_git_repo(
        repo_dir,
        {
            "Cargo.toml": '[package]\nname = "svc-rust"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub fn add(a: i32, b: i32) -> i32 { a + b }\n",
        },
    )

    # Populate a fake 64 KiB `target/` directory
    target_dir = repo_dir / "target" / "debug"
    target_dir.mkdir(parents=True)
    (target_dir / "libsvc_rust.rlib").write_bytes(b"x" * 65536)

    # Create two linked git worktrees: one clean & stale (48h old), one dirty & stale (48h old)
    stale_clean_wt = tmp_path / "wt-stale-clean"
    stale_dirty_wt = tmp_path / "wt-stale-dirty"
    subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "add", "-b", "tmp/clean", str(stale_clean_wt), "HEAD"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "add", "-b", "tmp/dirty", str(stale_dirty_wt), "HEAD"],
        check=True,
        capture_output=True,
    )
    (stale_dirty_wt / "UNCOMMITTED.txt").write_text("user work in progress\n", encoding="utf-8")

    old_ts = time.time() - (48 * 3600)
    os.utime(stale_clean_wt, (old_ts, old_ts))
    os.utime(stale_dirty_wt, (old_ts, old_ts))

    repos = {"svc-rust": NightwatchRepoSpec(name="svc-rust", path=str(repo_dir))}

    wt_res = clean_stale_repo_worktrees(repos, stale_hours=24.0, execution_mode="live")
    assert len(wt_res["removedWorktrees"]) == 1
    assert wt_res["removedWorktrees"][0]["path"] == str(stale_clean_wt)
    assert not stale_clean_wt.exists(), "Stale clean worktree should be removed"
    assert stale_dirty_wt.exists(), "Dirty worktree with uncommitted edits MUST be preserved!"
    assert str(stale_dirty_wt) in wt_res["skippedDirtyWorktrees"]
    assert repo_dir.exists(), "Primary checkout MUST be preserved!"

    # Test Cargo `target/` deletion (patching `shutil.which('cargo')` to None so direct rmtree path is verified deterministically)
    with patch("nightwatch.hygiene.shutil.which", return_value=None):
        cargo_res = reclaim_cargo_targets(repos, extra_target_dirs=[], execution_mode="live")
    assert cargo_res["bytesReclaimed"] >= 65536
    assert not (repo_dir / "target").exists()


def test_docker_prune_parses_reclaimed_space_and_skips_in_cloud() -> None:
    # 1. Skipped cleanly in cloud
    cloud_res = reclaim_docker_images(rippflow_env="cloud", prune_until="72h", execution_mode="live")
    assert cloud_res["status"] == "skipped_in_cloud"
    assert cloud_res["bytesReclaimed"] == 0

    # 2. Parses reclaimed space in live local mode
    def _fake_runner(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        if "df" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout='{"Type":"Images","Reclaimable":"1.5GB"}\n', stderr="")
        if "image" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="Total reclaimed space: 1.5GB\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="Total reclaimed space: 250MB\n", stderr="")

    live_res = reclaim_docker_images(
        rippflow_env="local",
        prune_until="72h",
        execution_mode="live",
        which_fn=lambda _: "/usr/local/bin/docker",
        runner=_fake_runner,
    )
    assert live_res["status"] == "pruned"
    assert live_res["bytesReclaimed"] == 1_500_000_000 + 250_000_000


def test_flaky_test_detector_identifies_intermittent_test_cases(tmp_path: Path) -> None:
    script = tmp_path / "run_tests.py"
    script.write_text(
        "import os, sys\n"
        "idx = int(os.environ.get('NIGHTWATCH_TEST_PASS_INDEX', '1'))\n"
        "if idx == 1:\n"
        "    print('FAILED tests/test_net.py::test_socket_race - TimeoutError')\n"
        "    sys.exit(1)\n"
        "print('2 passed')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    passed, flaky = detect_flaky_tests(
        tmp_path,
        ecosystem="python",
        explicit_cmd=[sys.executable, str(script)],
        runs=2,
        env=os.environ.copy(),
    )
    assert passed is True
    assert flaky == ["tests/test_net.py::test_socket_race"]


def test_cloud_env_strictly_flags_off_ai_upgrader_and_lambda_adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RIPPFLOW_ENV", "cloud")
    assert detect_rippflow_env() == "cloud"

    cfg = NightwatchConfig.from_raw(
        {
            "workspaceDir": str(tmp_path),
            "enableAiUpgrader": True,
            "upgraderProvider": "codex",
            "repos": ["svc-a"],
        },
        workflow_dir=WORKFLOW_DIR,
    )
    assert cfg.ai_upgrader_enabled is False
    assert cfg.upgrader_provider == "deterministic"

    wt = tmp_path / "svc-a"
    _init_git_repo(wt, {"Cargo.toml": '[package]\nname = "svc-a"\nversion = "0.1.0"\n\n[dependencies]\nserde = "1.0.190"\n'})

    with (
        patch("nightwatch.upgrader._try_codex_breaking_fix", side_effect=AssertionError("Codex must NOT run in cloud!")),
        patch("nightwatch.upgrader._try_gemini_breaking_fix_async", side_effect=AssertionError("Gemini must NOT run in cloud!")),
    ):
        modified, used, _summary = upgrade_and_adapt_worktree(
            wt=wt,
            ecosystem="rust",
            outdated_packages=[{"name": "serde", "current": "1.0.190", "latest": "1.0.215"}],
            advisories=[{"id": "RUSTSEC-0000-0000", "package": "serde", "version": "1.0.190", "title": "test"}],
            compiler_warnings_sample=["unused import"],
            explicit_upgrade_cmd=None,
            verify_test_cmd=[sys.executable, "-c", "import sys; sys.exit(1)"],
            rippflow_env="cloud",
            ai_enabled=True,
            provider="auto",
            gemini_model="gemini-3.8-flash",
            codex_model="gpt-6.1-sol",
            env=os.environ.copy(),
        )
    assert used == "deterministic:cloud-safe"
    assert "Cargo.toml" in modified
    assert 'serde = "1.0.215"' in (wt / "Cargo.toml").read_text(encoding="utf-8")

    # Verify AWS Lambda `lambda_handler` inline routing
    monkeypatch.setenv("RIPPFLOW_STATE_DIR", str(tmp_path / "lambda_runs"))
    out = lambda_handler(
        {
            "phase": "reclaim_disk_and_worktrees",
            "requestId": "wf-nw-lambda-1",
            "payload": {
                "workspaceDir": str(tmp_path),
                "executionMode": "dry_run",
                "repos": [{"name": "svc-a", "path": str(wt)}],
            },
        },
        workflow_dir=WORKFLOW_DIR,
    )
    assert out["outcome"] == "completed"
    assert out["payload"]["aiUpgraderEnabled"] is False


def test_live_rippflow_run_nightwatch_end_to_end_reclaim_upgrade_benchmark_and_digest(tmp_path: Path) -> None:
    """Runs the real `rippflow` engine end-to-end across 2 shards (`svc-fast` green upgrade + `svc-regressed` benchmark regression):
    - Reclaims stale git worktree + Cargo `target/` bytes in Phase 1
    - Audits advisories, outdated packages, compiler warnings, and flaky tests in Phase 3 (`async_tool`)
    - Upgrades manifests in isolated worktrees in Phase 4 (`async_tool`)
    - Verifies benchmark delta (`svc-fast` +2% passes -> green ready-to-merge PR link; `svc-regressed` +25% flagged!) in Phase 5
    - Delivers Slack webhook + `MORNING_DIGEST.md` in Phase 6 (`morning_digest_and_notify`)
    """
    rippflow_bin = _find_rippflow_bin()
    if not rippflow_bin:
        pytest.skip("rippflow CLI binary not installed on PATH")

    ws = tmp_path / "workspace"
    _init_git_repo(
        ws / "svc-fast",
        {
            "Cargo.toml": '[package]\nname = "svc-fast"\nversion = "0.1.0"\n\n[dependencies]\ntokio = "1.36.0"\n',
            "src/lib.rs": "pub fn run() -> bool { true }\n",
        },
    )
    # Create a 32 KiB `target/` dir + stale clean worktree on `svc-fast` to prove Phase 1 reclaims them!
    (ws / "svc-fast" / "target").mkdir()
    (ws / "svc-fast" / "target" / "artifact.bin").write_bytes(b"a" * 32768)
    stale_wt = tmp_path / "old-worktree"
    subprocess.run(
        ["git", "-C", str(ws / "svc-fast"), "worktree", "add", "-b", "old/wt", str(stale_wt), "HEAD"],
        check=True,
        capture_output=True,
    )
    old_ts = time.time() - (36 * 3600)
    os.utime(stale_wt, (old_ts, old_ts))

    _init_git_repo(
        ws / "svc-regressed",
        {
            "package.json": json.dumps({"name": "svc-regressed", "version": "0.1.0", "dependencies": {"zod": "^3.21.0"}}, indent=2) + "\n",
        },
    )

    slack_messages: list[dict[str, Any]] = []

    class _SlackStub(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length", "0"))
            slack_messages.append(json.loads(self.rfile.read(n).decode("utf-8")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), _SlackStub)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    py = sys.executable
    input_file = tmp_path / "nw_input.json"
    report_file = tmp_path / "nw_report.json"
    input_file.write_text(
        json.dumps(
            {
                "workspaceDir": str(ws),
                "executionMode": "live",
                "enableAiUpgrader": True,  # `--rippflow-env cloud` must force this OFF!
                "enableOsNotifications": False,
                "slackWebhookUrl": f"http://127.0.0.1:{port}/slack",
                "flakyTestRuns": 2,
                "maxBenchmarkRegressionPct": 5.0,
                "hygiene": {
                    "cleanStaleWorktrees": True,
                    "staleWorktreeHours": 24,
                    "deleteCargoTargets": True,
                    "cleanDockerImages": False,
                },
                "repos": [
                    {
                        "name": "svc-fast",
                        "path": str(ws / "svc-fast"),
                        "auditCommand": [py, "-c", 'print(\'[{"id":"RUSTSEC-2026-0001","package":"tokio","version":"1.36.0","title":"timer fix"}]\')'],
                        "outdatedCommand": [py, "-c", 'print(\'[{"name":"tokio","current":"1.36.0","latest":"1.41.0"}]\')'],
                        "warningsCommand": [py, "-c", "print('warning: unused variable `x`')"],
                        "testCommand": [py, "-c", "print('all tests green')"],
                        "benchmarkCommand": [
                            py,
                            "-c",
                            "import os; r=os.environ.get('NIGHTWATCH_BENCH_ROLE'); print('p99=10.0ms' if r=='baseline' else 'p99=10.2ms')",
                        ],
                    },
                    {
                        "name": "svc-regressed",
                        "path": str(ws / "svc-regressed"),
                        "outdatedCommand": [py, "-c", 'print(\'[{"name":"zod","current":"3.21.0","latest":"3.24.0"}]\')'],
                        "testCommand": [
                            py,
                            "-c",
                            "import os,sys; i=os.environ.get('NIGHTWATCH_TEST_PASS_INDEX','2'); "
                            "print('FAILED tests/t.py::test_flake' if i=='1' else 'ok'); sys.exit(1 if i=='1' else 0)",
                        ],
                        "benchmarkCommand": [
                            py,
                            "-c",
                            "import os; r=os.environ.get('NIGHTWATCH_BENCH_ROLE'); print('p99=10.0ms' if r=='baseline' else 'p99=13.0ms')",
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    run_id: str | None = None
    try:
        proc = subprocess.run(
            [
                rippflow_bin,
                "run",
                "workflow.yaml",
                "--handlers",
                "handlers.json",
                "--rippflow-env",
                "cloud",
                "--shards",
                "svc-fast,svc-regressed",
                "--input",
                str(input_file),
                "--report",
                str(report_file),
                "--timeout-seconds",
                "120",
                "--json",
            ],
            cwd=str(WORKFLOW_DIR),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        report = json.loads(report_file.read_text(encoding="utf-8"))
        assert report["status"] == "completed"
        run_id = str(report["requestId"])

        # Verify worker capacity was released during all 3 async_tool phases!
        suspended = {
            ev.get("phase")
            for ev in report.get("events", [])
            if ev.get("event") == "async_tool_suspended" and ev.get("workerCapacityReleased") is True
        }
        assert {"audit_deps_and_warnings", "upgrade_and_fix_breaking_apis", "benchmark_regression_check"}.issubset(suspended)

        # Verify Phase 1 deleted the stale worktree and reclaimed Cargo target bytes!
        assert not stale_wt.exists()
        assert not (ws / "svc-fast" / "target").exists()

        digest = json.loads((WORKFLOW_DIR / ".runs" / run_id / "digest.json").read_text(encoding="utf-8"))
        assert digest["rippflowEnv"] == "cloud"
        assert digest["aiUpgraderEnabled"] is False
        assert digest["reclaimedBytes"] >= 32768
        assert len(digest["greenPrLinks"]) == 1
        assert "svc-fast" in digest["greenPrLinks"][0]
        assert "svc-regressed" in digest["flaggedShards"]

        by_shard = {s["shard"]: s for s in digest["shards"]}
        assert by_shard["svc-fast"]["greenReadyToMerge"] is True
        assert by_shard["svc-fast"]["upgraderUsed"] == "deterministic:cloud-safe"
        assert by_shard["svc-fast"]["benchmarkDeltaPct"] == 2.0
        assert by_shard["svc-fast"]["advisoriesCount"] == 1
        assert by_shard["svc-fast"]["compilerWarningsCount"] == 1

        assert by_shard["svc-regressed"]["greenReadyToMerge"] is False
        assert by_shard["svc-regressed"]["benchmarkPassed"] is False
        assert by_shard["svc-regressed"]["benchmarkDeltaPct"] == 30.0
        assert by_shard["svc-regressed"]["flakyTests"] == ["tests/t.py::test_flake"]

        assert len(slack_messages) == 1
        assert "1 green PR(s) ready" in slack_messages[0]["text"]
        assert (WORKFLOW_DIR / ".runs" / run_id / "MORNING_DIGEST.md").is_file()
    finally:
        server.shutdown()
        if run_id:
            subprocess.run(
                [py, "scripts/dispatch.py", "--cleanup-run", "--run-id", run_id],
                cwd=str(WORKFLOW_DIR),
                check=False,
                capture_output=True,
            )
            shutil.rmtree(WORKFLOW_DIR / ".runs" / run_id, ignore_errors=True)
