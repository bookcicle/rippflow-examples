"""Morning digest OS desktop notifications (`local`) + Slack/email webhooks (`local` & `cloud`)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
import urllib.error
import urllib.request
from xml.sax.saxutils import escape as xml_escape

from .callback_client import redact_secrets


@dataclass(frozen=True)
class MorningDigestAlert:
    title: str
    subtitle: str
    body: str
    run_id: str
    reclaimed_human: str
    green_prs: list[str]
    flagged_shards: list[str]


@dataclass(frozen=True)
class NotificationReceipt:
    channel: str
    delivered: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _truncate(text: str, limit: int) -> str:
    cleaned = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    return cleaned if len(cleaned) <= limit else cleaned[: max(0, limit - 1)].rstrip() + "…"


def _applescript_quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _powershell_single_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _run_cmd(argv: list[str], timeout_sec: float = 5.0) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        if proc.returncode == 0:
            return True, (proc.stdout or "ok").strip()
        return False, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
    except Exception as exc:
        return False, str(exc)


def send_desktop_notification(
    alert: MorningDigestAlert,
    *,
    platform_override: str | None = None,
    which_fn=shutil.which,
    runner=_run_cmd,
) -> NotificationReceipt:
    plat = (platform_override or sys.platform).lower()
    title = _truncate(alert.title, 80)
    subtitle = _truncate(alert.subtitle, 100)
    body = _truncate(alert.body, 220)

    if plat.startswith("darwin"):
        tn = which_fn("terminal-notifier")
        if tn:
            cmd = [tn, "-title", title, "-subtitle", subtitle, "-message", body, "-sound", "Glass"]
            if alert.green_prs:
                cmd.extend(["-open", alert.green_prs[0]])
            ok, detail = runner(cmd)
            if ok:
                return NotificationReceipt("macos:terminal-notifier", True, detail)
        osa = which_fn("osascript")
        if osa:
            script = (
                f"display notification {_applescript_quote(body)} "
                f"with title {_applescript_quote(title)} "
                f"subtitle {_applescript_quote(subtitle)} sound name \"Glass\""
            )
            ok, detail = runner([osa, "-e", script])
            if ok:
                return NotificationReceipt("macos:osascript", True, detail)

    elif plat.startswith("linux"):
        ns = which_fn("notify-send")
        if ns:
            ok, detail = runner([ns, "-u", "normal", "-a", "Rippflow Nightwatch", f"{title} — {subtitle}", body])
            if ok:
                return NotificationReceipt("linux:notify-send", True, detail)

    elif plat.startswith("win"):
        ps = which_fn("powershell.exe") or which_fn("pwsh") or which_fn("powershell")
        if ps:
            t_xml = xml_escape(f"{title} — {subtitle}")
            b_xml = xml_escape(body)
            toast_xml = f"<toast><visual><binding template='ToastGeneric'><text>{t_xml}</text><text>{b_xml}</text></binding></visual></toast>"
            ps_cmd = (
                "$ErrorActionPreference = 'Stop'; "
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
                "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null; "
                "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
                f"$xml.LoadXml({_powershell_single_quote(toast_xml)}); "
                "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Rippflow').Show($toast)"
            )
            ok, detail = runner([ps, "-NoProfile", "-NonInteractive", "-Command", ps_cmd])
            if ok:
                return NotificationReceipt("windows:winrt-toast", True, detail)

    return NotificationReceipt("audit-log-fallback", True, f"Recorded morning digest for {plat}")


def _post_webhook(url: str, payload: dict[str, Any], *, opener=urllib.request.urlopen) -> tuple[bool, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "rippflow-nightwatch/1.0"},
        method="POST",
    )
    try:
        with opener(req, timeout=10.0) as resp:
            code = getattr(resp, "status", 200)
            return 200 <= code < 300, f"HTTP {code}"
    except urllib.error.HTTPError as err:
        return False, f"HTTP {err.code}"
    except Exception as exc:
        return False, redact_secrets(str(exc))


def dispatch_morning_digest_notifications(
    alert: MorningDigestAlert,
    *,
    rippflow_env: str,
    enable_os_notifications: bool = True,
    slack_webhook_url: str | None = None,
    email_webhook_url: str | None = None,
    alert_log_path: Path | None = None,
    platform_override: str | None = None,
    which_fn=shutil.which,
    runner=_run_cmd,
    webhook_opener=urllib.request.urlopen,
) -> list[NotificationReceipt]:
    is_cloud = rippflow_env.strip().lower() == "cloud"
    receipts: list[NotificationReceipt] = []

    if (not is_cloud) and enable_os_notifications:
        receipts.append(
            send_desktop_notification(
                alert,
                platform_override=platform_override,
                which_fn=which_fn,
                runner=runner,
            )
        )
    else:
        receipts.append(
            NotificationReceipt(
                channel="desktop:skipped_in_cloud" if is_cloud else "desktop:disabled",
                delivered=False,
                detail="Skipped desktop banner",
            )
        )

    if slack_webhook_url and slack_webhook_url.strip():
        pr_lines = "\n".join(f"• {u}" for u in alert.green_prs) if alert.green_prs else "• _No new upgrade PRs needed_"
        slack_payload = {
            "text": f"*{alert.title}* — {alert.subtitle}\n{alert.body}\n{pr_lines}",
        }
        ok, detail = _post_webhook(slack_webhook_url.strip(), slack_payload, opener=webhook_opener)
        receipts.append(NotificationReceipt("webhook:slack", ok, detail))

    if email_webhook_url and email_webhook_url.strip():
        email_payload = {
            "subject": f"[Rippflow Nightwatch] {alert.subtitle}",
            "runId": alert.run_id,
            "reclaimedDisk": alert.reclaimed_human,
            "greenPrLinks": alert.green_prs,
            "flaggedShards": alert.flagged_shards,
            "summary": alert.body,
        }
        ok, detail = _post_webhook(email_webhook_url.strip(), email_payload, opener=webhook_opener)
        receipts.append(NotificationReceipt("webhook:email", ok, detail))

    if alert_log_path is not None:
        alert_log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "rippflowEnv": rippflow_env,
            "runId": alert.run_id,
            "title": alert.title,
            "subtitle": alert.subtitle,
            "body": alert.body,
            "greenPrs": alert.green_prs,
            "receipts": [r.to_dict() for r in receipts],
        }
        with open(alert_log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    return receipts
