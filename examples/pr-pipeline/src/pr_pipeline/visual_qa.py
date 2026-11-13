"""Visual mock rendering, PR mock-screenshot pinning, and UI screenshot capture helpers.

Enables closed-loop visual review in `multi-repo-feature-pr-pipeline`:
1. Renders HTML design mocks (`mockHtml` / `mockScreenshots`) into deterministic PNGs
   via native macOS `WKWebView` (zero screen-recording permission or GUI window popup)
   or headless Chromium/Playwright on Linux/Windows.
2. Commits & pushes reference mock PNGs to a dedicated orphan branch (`_pr-visual-assets`)
   under `authorGhUser` credentials and pins a `## Visual Design Reference (Pinned Mock)`
   section onto the GitHub PR description + review comment so both human and AI reviewers
   can inspect the exact target UI directly on the PR.
3. Runs `screenshotCommand` in `local_test_suite` to capture rendered UI PNGs from the
   implementation (e.g. GPUI `HeadlessAppContext::capture_screenshot` Metal offscreen render)
   and attaches BOTH mock PNGs and actual UI PNGs via `codex exec -i <img1> -i <img2> ...`
   during `codex_review` and rework `author_pr` turns.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from typing import Any


PINNED_MOCK_MARKER_START = "<!-- rippflow-visual-mock:start -->"
PINNED_MOCK_MARKER_END = "<!-- rippflow-visual-mock:end -->"
VISUAL_ASSETS_BRANCH = "_pr-visual-assets"

DEFAULT_DARK_THEME_VARS_CSS = """
    :root {
      --background: #051312;
      --content: #061715;
      --card: #081e1c;
      --sidebar: #071917;
      --border: rgba(45, 212, 191, 0.18);
      --foreground: #ecfdf5;
      --muted-foreground: #8fb3ab;
      --placeholder: #5f877f;
      --primary: #10b981;
      --primary-foreground: #032019;
      --secondary: rgba(16, 185, 129, 0.10);
      --secondary-foreground: #d1fae5;
      --accent: #14b8a6;
    }
"""

_SWIFT_WKWEBVIEW_SOURCE = r"""
import AppKit
import WebKit

let args = CommandLine.arguments
guard args.count >= 3 else {
    fputs("Usage: render_html_png <input.html> <output.png> [width] [height] [eval_js]\n", stderr)
    exit(2)
}

let htmlPath = args[1]
let outPath = args[2]
let width = args.count >= 4 ? (Double(args[3]) ?? 1440.0) : 1440.0
let height = args.count >= 5 ? (Double(args[4]) ?? 940.0) : 940.0
let extraJs = args.count >= 6 ? args[5] : ""

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)

class SnapshotDelegate: NSObject, WKNavigationDelegate {
    let webView: WKWebView
    let outUrl: URL
    let width: Double
    let height: Double
    let extraJs: String

    init(webView: WKWebView, outUrl: URL, width: Double, height: Double, extraJs: String) {
        self.webView = webView
        self.outUrl = outUrl
        self.width = width
        self.height = height
        self.extraJs = extraJs
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        let stepSnapshot = {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.45) {
                let cfg = WKSnapshotConfiguration()
                cfg.rect = CGRect(x: 0, y: 0, width: self.width, height: self.height)
                webView.takeSnapshot(with: cfg) { image, err in
                    if let err = err {
                        fputs("Snapshot error: \(err)\n", stderr)
                        exit(1)
                    }
                    guard let img = image,
                          let tiff = img.tiffRepresentation,
                          let rep = NSBitmapImageRep(data: tiff),
                          let png = rep.representation(using: .png, properties: [:]) else {
                        fputs("Failed to encode PNG\n", stderr)
                        exit(1)
                    }
                    do {
                        try png.write(to: self.outUrl)
                        exit(0)
                    } catch {
                        fputs("Write error: \(error)\n", stderr)
                        exit(1)
                    }
                }
            }
        }

        if !extraJs.isEmpty {
            webView.evaluateJavaScript(extraJs) { _, _ in
                stepSnapshot()
            }
        } else {
            stepSnapshot()
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        fputs("Navigation error: \(error)\n", stderr)
        exit(1)
    }
}

let frame = CGRect(x: 0, y: 0, width: width, height: height)
let config = WKWebViewConfiguration()
let webView = WKWebView(frame: frame, configuration: config)
webView.appearance = NSAppearance(named: .darkAqua)

let htmlUrl = URL(fileURLWithPath: htmlPath)
let outUrl = URL(fileURLWithPath: outPath)
let delegate = SnapshotDelegate(webView: webView, outUrl: outUrl, width: width, height: height, extraJs: extraJs)
webView.navigationDelegate = delegate
webView.loadFileURL(htmlUrl, allowingReadAccessTo: htmlUrl.deletingLastPathComponent())

DispatchQueue.main.asyncAfter(deadline: .now() + 15.0) {
    fputs("Timed out rendering \(htmlPath)\n", stderr)
    exit(1)
}

app.run()
"""


def _ensure_macos_wkwebview_helper(cache_dir: Path) -> Path | None:
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    bin_path = cache_dir / "render_html_png"
    src_path = cache_dir / "render_html_png.swift"
    if bin_path.is_file() and os.access(bin_path, os.X_OK):
        return bin_path
    src_path.write_text(_SWIFT_WKWEBVIEW_SOURCE, encoding="utf-8")
    proc = subprocess.run(
        ["swiftc", "-O", str(src_path), "-o", str(bin_path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if proc.returncode == 0 and bin_path.is_file():
        return bin_path
    return None


def _stage_html_with_offline_tailwind(html_path: Path, stage_dir: Path) -> Path:
    """Stages an HTML mock file alongside a cached Tailwind bundle and semantic CSS variables
    so `file://` snapshots render faithfully even when internal CDN hosts are unreachable.
    """
    stage_dir.mkdir(parents=True, exist_ok=True)
    html_text = html_path.read_text(encoding="utf-8")

    if "tailwindcss" in html_text:
        tw_bundle = stage_dir / "tailwind_cdn.js"
        if not tw_bundle.is_file():
            try:
                req = urllib.request.Request(
                    "https://cdn.tailwindcss.com/3.4.17",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    tw_bundle.write_bytes(resp.read())
            except Exception:
                pass
        if tw_bundle.is_file():
            html_text = re.sub(
                r'https://[^\s"\'<>]*tailwind[^\s"\'<>]*\.js',
                f"file://{tw_bundle}",
                html_text,
            )

    if "var(--background)" in html_text and "--background:" not in html_text:
        if "<style>" in html_text:
            html_text = html_text.replace("<style>", f"<style>\n{DEFAULT_DARK_THEME_VARS_CSS}", 1)
        elif "</head>" in html_text:
            html_text = html_text.replace(
                "</head>", f"<style>{DEFAULT_DARK_THEME_VARS_CSS}</style>\n</head>", 1
            )

    staged = stage_dir / f"_staged_{html_path.name}"
    staged.write_text(html_text, encoding="utf-8")
    return staged


def render_html_mock_screenshots(
    *,
    html_path: Path,
    output_dir: Path,
    views: list[dict[str, str]] | None = None,
    width: int = 1440,
    height: int = 940,
) -> list[Path]:
    """Renders one or more views of an HTML mock file into PNG screenshots."""
    output_dir.mkdir(parents=True, exist_ok=True)
    html_text = html_path.read_text(encoding="utf-8")

    # Auto-detect Rippflow unified monitor multi-tab mocks (`setActiveTab('runs'|'logs'|'resources')`)
    if views is None:
        if "function setActiveTab(" in html_text and "view-runs" in html_text and "view-logs" in html_text:
            views = [
                {"name": "mock_01_runs_view.png", "js": "setActiveTab('runs'); true;"},
                {"name": "mock_02_logs_view.png", "js": "setActiveTab('logs'); true;"},
                {"name": "mock_03_resources_view.png", "js": "setActiveTab('resources'); true;"},
            ]
        else:
            views = [{"name": f"mock_{html_path.stem}.png", "js": ""}]

    staged_html = _stage_html_with_offline_tailwind(html_path, output_dir / ".stage")
    wk_bin = _ensure_macos_wkwebview_helper(Path(tempfile.gettempdir()) / "rippflow-wkwebview-bin")

    rendered: list[Path] = []
    for item in views:
        out_png = output_dir / item["name"]
        js_expr = item.get("js", "")
        if wk_bin is not None:
            proc = subprocess.run(
                [str(wk_bin), str(staged_html), str(out_png), str(width), str(height), js_expr],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
            if proc.returncode == 0 and out_png.is_file() and out_png.stat().st_size > 0:
                rendered.append(out_png)
                continue

        # Fallback: headless Chromium / Chrome on Linux or Windows
        chrome_bin = (
            shutil.which("chromium")
            or shutil.which("chromium-browser")
            or shutil.which("google-chrome")
            or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )
        if chrome_bin and Path(chrome_bin).exists():
            patched_html = staged_html
            if js_expr:
                patched_html = output_dir / ".stage" / f"_patched_{out_png.stem}.html"
                patched_html.write_text(
                    staged_html.read_text(encoding="utf-8").replace(
                        "</body>", f"<script>{js_expr}</script></body>"
                    ),
                    encoding="utf-8",
                )
            with tempfile.TemporaryDirectory(prefix="rf-chrome-mock-") as prof:
                subprocess.run(
                    [
                        chrome_bin,
                        f"--user-data-dir={prof}",
                        "--headless=new",
                        "--no-first-run",
                        "--disable-gpu",
                        "--hide-scrollbars",
                        f"--window-size={width},{height}",
                        f"--screenshot={out_png}",
                        f"file://{patched_html}",
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
            if out_png.is_file() and out_png.stat().st_size > 0:
                rendered.append(out_png)

    return rendered


def collect_image_files(paths_or_dirs: list[str | Path]) -> list[Path]:
    """Resolves explicit image files or directories of `.png`/`.jpg`/`.webp` screenshots."""
    found: list[Path] = []
    seen: set[Path] = set()
    exts = {".png", ".jpg", ".jpeg", ".webp"}
    for raw in paths_or_dirs:
        p = Path(raw).expanduser()
        if p.is_file() and p.suffix.lower() in exts:
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                found.append(rp)
        elif p.is_dir():
            for child in sorted(p.iterdir()):
                if child.is_file() and child.suffix.lower() in exts and not child.name.startswith("."):
                    rp = child.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        found.append(rp)
    return found


def prepare_reference_mock_images(
    *,
    mock_screenshots: list[str],
    mock_html: str | None,
    dest_dir: Path,
) -> list[Path]:
    """Materializes reference mock screenshots into `dest_dir` so they persist across all turns."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    collected: list[Path] = []

    for idx, src in enumerate(collect_image_files(mock_screenshots), start=1):
        target = dest_dir / f"mock_{idx:02d}_{src.name}"
        if src.resolve() != target.resolve():
            shutil.copy2(src, target)
        collected.append(target)

    if mock_html:
        html_file = Path(mock_html).expanduser().resolve()
        if html_file.is_file():
            rendered = render_html_mock_screenshots(html_path=html_file, output_dir=dest_dir)
            for p in rendered:
                if p not in collected:
                    collected.append(p)

    return sorted({p.resolve() for p in collected if p.is_file()})


def _push_images_to_visual_assets_branch(
    *,
    images: list[Path],
    github_repo: str,
    asset_subdir: str,
    author_gh_user: str,
    author_git_email: str | None,
    author_env: dict[str, str],
) -> list[tuple[str, str]]:
    """Commits `images` onto orphan branch `_pr-visual-assets` (zero pollution in `main`/PR diff)
    and returns `(label, github_blob_raw_url)` pairs that render inline in GitHub PR Markdown!
    """
    if not images or not github_repo:
        return []

    gh_bin = shutil.which("gh") or "gh"
    push_url = f"https://github.com/{github_repo}.git"
    email = author_git_email or f"{author_gh_user}@users.noreply.github.com"

    with tempfile.TemporaryDirectory(prefix="rf-pr-visual-assets-") as tmp:
        tmp_repo = Path(tmp) / "assets"
        tmp_repo.mkdir(parents=True, exist_ok=True)
        git_base = [
            "git",
            "-C",
            str(tmp_repo),
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper=!{gh_bin} auth git-credential",
        ]
        subprocess.run(["git", "init", "-b", VISUAL_ASSETS_BRANCH, str(tmp_repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(tmp_repo), "config", "user.name", author_gh_user], check=True)
        subprocess.run(["git", "-C", str(tmp_repo), "config", "user.email", email], check=True)
        subprocess.run(["git", "-C", str(tmp_repo), "config", "commit.gpgsign", "false"], check=True)

        # Pull existing `_pr-visual-assets` if present so earlier PRs' pinned images stay intact
        fetch = subprocess.run(
            [*git_base, "fetch", "--depth=1", push_url, VISUAL_ASSETS_BRANCH],
            env=author_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if fetch.returncode == 0:
            subprocess.run(
                ["git", "-C", str(tmp_repo), "reset", "--hard", "FETCH_HEAD"],
                env=author_env,
                capture_output=True,
                check=False,
            )

        rel_items: list[tuple[str, str]] = []
        target_dir = tmp_repo / asset_subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        for img in images:
            safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", img.name)
            shutil.copy2(img, target_dir / safe_name)
            rel_path = f"{asset_subdir}/{safe_name}"
            # `?raw=true` on `/blob/` authenticates seamlessly via GitHub browser cookies even on private repos!
            url = f"https://github.com/{github_repo}/blob/{VISUAL_ASSETS_BRANCH}/{rel_path}?raw=true"
            rel_items.append((safe_name, url))

        subprocess.run(["git", "-C", str(tmp_repo), "add", "-A"], env=author_env, check=True)
        status = subprocess.run(
            ["git", "-C", str(tmp_repo), "status", "--porcelain"],
            env=author_env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if status:
            subprocess.run(
                ["git", "-C", str(tmp_repo), "commit", "-m", f"chore(visual-qa): pin UI screenshots ({asset_subdir})"],
                env=author_env,
                capture_output=True,
                text=True,
                check=True,
            )
            push = subprocess.run(
                [*git_base, "push", push_url, f"HEAD:refs/heads/{VISUAL_ASSETS_BRANCH}"],
                env=author_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if push.returncode != 0:
                # Non-fast-forward race fallback: re-fetch & rebase once
                subprocess.run(
                    [*git_base, "pull", "--rebase", push_url, VISUAL_ASSETS_BRANCH],
                    env=author_env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=False,
                )
                subprocess.run(
                    [*git_base, "push", push_url, f"HEAD:refs/heads/{VISUAL_ASSETS_BRANCH}"],
                    env=author_env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=True,
                )

    return rel_items


def pin_mock_screenshots_to_pr(
    *,
    pr_url_or_number: str,
    github_repo: str | None,
    worktree: Path,
    mock_images: list[Path],
    actual_images: list[Path] | None,
    turn: int,
    author_gh_user: str,
    author_git_email: str | None,
    author_env: dict[str, str],
) -> list[str]:
    """Uploads reference mock screenshots (and latest rendered UI screenshots, when available)
    to `_pr-visual-assets` and pins/updates the `## Visual Design Reference (Pinned Mock)` block
    at the top of the GitHub PR description so reviewers always compare side-by-side.
    """
    if not mock_images and not actual_images:
        return []
    if not github_repo:
        return []

    repo_args = ["--repo", github_repo] if not str(pr_url_or_number).startswith("http") else []
    view = subprocess.run(
        ["gh", "pr", "view", str(pr_url_or_number), *repo_args, "--json", "number,body"],
        cwd=str(worktree),
        env=author_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if view.returncode != 0 or not view.stdout.strip():
        return []
    pr_info = json.loads(view.stdout)
    pr_num = int(pr_info["number"])
    current_body = str(pr_info.get("body") or "")

    uploaded_urls: list[str] = []
    md_lines: list[str] = [
        PINNED_MOCK_MARKER_START,
        "## Visual Design Reference (Pinned Mock vs. Rendered UI)",
        "",
    ]

    if mock_images:
        mock_pairs = _push_images_to_visual_assets_branch(
            images=mock_images,
            github_repo=github_repo,
            asset_subdir=f"pr-{pr_num}/mock",
            author_gh_user=author_gh_user,
            author_git_email=author_git_email,
            author_env=author_env,
        )
        if mock_pairs:
            md_lines.append("### Target Reference Mock (Source of Truth)")
            for label, url in mock_pairs:
                uploaded_urls.append(url)
                md_lines.append(f"**`{label}`**")
                md_lines.append(f"![{label}]({url})")
                md_lines.append("")

    if actual_images:
        actual_pairs = _push_images_to_visual_assets_branch(
            images=actual_images,
            github_repo=github_repo,
            asset_subdir=f"pr-{pr_num}/turn-{turn}",
            author_gh_user=author_gh_user,
            author_git_email=author_git_email,
            author_env=author_env,
        )
        if actual_pairs:
            md_lines.append(f"### Latest Rendered Implementation Screenshots (Turn {turn})")
            for label, url in actual_pairs:
                uploaded_urls.append(url)
                md_lines.append(f"**`{label}`**")
                md_lines.append(f"![{label}]({url})")
                md_lines.append("")

    md_lines.append(PINNED_MOCK_MARKER_END)
    pinned_block = "\n".join(md_lines)

    if PINNED_MOCK_MARKER_START in current_body and PINNED_MOCK_MARKER_END in current_body:
        pattern = re.compile(
            re.escape(PINNED_MOCK_MARKER_START) + r".*?" + re.escape(PINNED_MOCK_MARKER_END),
            re.DOTALL,
        )
        updated_body = pattern.sub(pinned_block, current_body, count=1)
    else:
        updated_body = f"{pinned_block}\n\n---\n\n{current_body}".strip()

    if updated_body != current_body:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as tf:
            tf.write(updated_body)
            body_file = Path(tf.name)
        try:
            subprocess.run(
                ["gh", "pr", "edit", str(pr_url_or_number), *repo_args, "--body-file", str(body_file)],
                cwd=str(worktree),
                env=author_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            body_file.unlink(missing_ok=True)

    return uploaded_urls
