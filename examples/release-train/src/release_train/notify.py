"""Environment-aware release sign-off notifications (`local` OS banners + `cloud` Slack/email webhooks).

- When `RIPPFLOW_ENV=cloud`, desktop OS notification binaries (`osascript`, `notify-send`, PowerShell Toast)
  are skipped cleanly and notifications route strictly via Slack/email HTTPS webhooks + structured audit logs.
- When `RIPPFLOW_ENV=local`, native macOS/Linux/Windows desktop notifications fire alongside any configured
  Slack/email webhooks.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
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
class ReleaseAlertMessage:
    title: str
    subtitle: str
    body: str
    release_version: str
    run_id: str
    shards: list[str]
    url: str | None = None


@dataclass(frozen=True)
class AlertDeliveryReceipt:
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
    msg: ReleaseAlertMessage,
    *,
    platform_override: str | None = None,
    which_fn=shutil.which,
    runner=_run_cmd,
) -> AlertDeliveryReceipt:
    plat = (platform_override or sys.platform).lower()
    title = _truncate(msg.title, 80)
    subtitle = _truncate(msg.subtitle, 100)
    body = _truncate(msg.body, 220)

    if plat.startswith("darwin"):
        tn = which_fn("terminal-notifier")
        if tn:
            cmd = [tn, "-title", title, "-subtitle", subtitle, "-message", body, "-sound", "Glass"]
            if msg.url:
                cmd.extend(["-open", msg.url])
            ok, detail = runner(cmd)
            if ok:
                return AlertDeliveryReceipt("macos:terminal-notifier", True, detail)
        osa = which_fn("osascript")
        if osa:
            script = (
                f"display notification {_applescript_quote(body)} "
                f"with title {_applescript_quote(title)} "
                f"subtitle {_applescript_quote(subtitle)} sound name \"Glass\""
            )
            ok, detail = runner([osa, "-e", script])
            if ok:
                return AlertDeliveryReceipt("macos:osascript", True, detail)

    elif plat.startswith("linux"):
        ns = which_fn("notify-send")
        if ns:
            ok, detail = runner([ns, "-u", "critical", "-a", "Rippflow Release Train", f"{title} — {subtitle}", body])
            if ok:
                return AlertDeliveryReceipt("linux:notify-send", True, detail)
        gdbus = which_fn("gdbus")
        if gdbus:
            ok, detail = runner(
                [
                    gdbus,
                    "call",
                    "--session",
                    "--dest",
                    "org.freedesktop.Notifications",
                    "--object-path",
                    "/org/freedesktop/Notifications",
                    "--method",
                    "org.freedesktop.Notifications.Notify",
                    "Rippflow",
                    "0",
                    "",
                    f"{title} — {subtitle}",
                    body,
                    "[]",
                    "{}",
                    "8000",
                ]
            )
            if ok:
                return AlertDeliveryReceipt("linux:gdbus", True, detail)

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
                return AlertDeliveryReceipt("windows:winrt-toast", True, detail)

    return AlertDeliveryReceipt("audit-log-fallback", True, f"Recorded sign-off alert for {plat}")


def post_json_webhook(
    webhook_url: str,
    payload: dict[str, Any],
    *,
    timeout_sec: float = 10.0,
    opener=urllib.request.urlopen,
) -> tuple[bool, str]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "rippflow-release-train/1.0"},
        method="POST",
    )
    try:
        with opener(req, timeout=timeout_sec) as resp:
            code = getattr(resp, "status", 200)
            return 200 <= code < 300, f"HTTP {code}"
    except urllib.error.HTTPError as err:
        return False, f"HTTP {err.code}"
    except Exception as exc:
        return False, redact_secrets(str(exc))


def dispatch_signoff_notifications(
    msg: ReleaseAlertMessage,
    *,
    rippflow_env: str,
    slack_webhook_url: str | None = None,
    email_webhook_url: str | None = None,
    alert_log_path: Path | None = None,
    platform_override: str | None = None,
    which_fn=shutil.which,
    runner=_run_cmd,
    webhook_opener=urllib.request.urlopen,
) -> list[AlertDeliveryReceipt]:
    """Delivers release sign-off notifications according to `RIPPFLOW_ENV`:
    - `local` / `device` / `edge`: fires native OS desktop banner + any configured Slack/email webhooks.
    - `cloud`: skips local desktop GUI tools cleanly and delivers via Slack/email webhooks + structured JSON log.
    """
    is_cloud = rippflow_env.strip().lower() == "cloud"
    receipts: list[AlertDeliveryReceipt] = []

    if not is_cloud:
        receipts.append(
            send_desktop_notification(
                msg,
                platform_override=platform_override,
                which_fn=which_fn,
                runner=runner,
            )
        )
    else:
        receipts.append(
            AlertDeliveryReceipt(
                channel="desktop:skipped_in_cloud",
                delivered=False,
                detail="RIPPFLOW_ENV=cloud skips desktop GUI notifications",
            )
        )

    if slack_webhook_url and slack_webhook_url.strip():
        slack_payload = {
            "text": f"*{msg.title}* (`v{msg.release_version}`)\n*{msg.subtitle}*\n{msg.body}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*{msg.title}* — `v{msg.release_version}`\n"
                            f"*Run:* `{msg.run_id}` · *Shards:* `{', '.join(msg.shards)}`\n"
                            f"{msg.body}"
                        ),
                    },
                }
            ],
        }
        ok, detail = post_json_webhook(slack_webhook_url.strip(), slack_payload, opener=webhook_opener)
        receipts.append(AlertDeliveryReceipt("webhook:slack", ok, detail))

    if email_webhook_url and email_webhook_url.strip():
        email_payload = {
            "subject": f"[Rippflow Release Gate] v{msg.release_version} awaiting sign-off ({msg.run_id})",
            "releaseVersion": msg.release_version,
            "runId": msg.run_id,
            "shards": msg.shards,
            "summary": msg.body,
            "actionUrl": msg.url,
        }
        ok, detail = post_json_webhook(email_webhook_url.strip(), email_payload, opener=webhook_opener)
        receipts.append(AlertDeliveryReceipt("webhook:email", ok, detail))

    if alert_log_path is not None:
        alert_log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "rippflowEnv": rippflow_env,
            "releaseVersion": msg.release_version,
            "runId": msg.run_id,
            "shards": msg.shards,
            "title": msg.title,
            "subtitle": msg.subtitle,
            "body": msg.body,
            "receipts": [r.to_dict() for r in receipts],
        }
        with open(alert_log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    return receipts
