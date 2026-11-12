"""Cross-platform desktop & terminal notification delivery for Rippflow workflows.

Supports macOS, Linux, Windows, and headless/SSH terminals with zero external
Python dependencies:
- macOS (`darwin`): `terminal-notifier` (clickable URL support) -> `osascript`
- Linux (`linux`): `notify-send` -> `gdbus` (`org.freedesktop.Notifications`) -> `kdialog`
- Windows (`win32`): PowerShell WinRT `ToastNotificationManager` -> WinForms `NotifyIcon`
- Universal fallback: OSC 9 desktop escape sequence on `/dev/tty` + JSON alert log
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
from xml.sax.saxutils import escape as xml_escape


@dataclass(frozen=True)
class NotificationMessage:
    title: str
    subtitle: str
    body: str
    url: str | None = None
    urgency: str = "critical"  # "low" | "normal" | "critical"
    sound: str = "Glass"


@dataclass(frozen=True)
class NotificationReceipt:
    delivered: bool
    backend: str
    platform: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _truncate(value: str, limit: int) -> str:
    cleaned = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def _applescript_quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _powershell_single_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _run_cmd(
    argv: list[str],
    timeout_sec: float = 5.0,
) -> tuple[bool, str]:
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
        err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        return False, err
    except Exception as exc:  # pragma: no cover - defensive OS boundary
        return False, str(exc)


def _send_macos(
    msg: NotificationMessage,
    *,
    which_fn=shutil.which,
    runner=_run_cmd,
) -> NotificationReceipt | None:
    title = _truncate(msg.title, 80)
    subtitle = _truncate(msg.subtitle, 100)
    body = _truncate(msg.body, 220)

    tn = which_fn("terminal-notifier")
    if tn:
        cmd = [tn, "-title", title, "-subtitle", subtitle, "-message", body]
        if msg.sound:
            cmd.extend(["-sound", msg.sound])
        if msg.url:
            cmd.extend(["-open", msg.url])
        ok, detail = runner(cmd)
        if ok:
            return NotificationReceipt(True, "macos:terminal-notifier", "darwin", detail)

    osa = which_fn("osascript")
    if osa:
        body_with_url = f"{body} ({msg.url})" if msg.url and msg.url not in body else body
        parts = [
            f"display notification {_applescript_quote(_truncate(body_with_url, 240))}",
            f"with title {_applescript_quote(title)}",
        ]
        if subtitle:
            parts.append(f"subtitle {_applescript_quote(subtitle)}")
        if msg.sound:
            parts.append(f"sound name {_applescript_quote(msg.sound)}")
        script = " ".join(parts)
        ok, detail = runner([osa, "-e", script])
        if ok:
            return NotificationReceipt(True, "macos:osascript", "darwin", detail)

    return None


def _send_linux(
    msg: NotificationMessage,
    *,
    which_fn=shutil.which,
    runner=_run_cmd,
) -> NotificationReceipt | None:
    title = _truncate(
        f"{msg.title} — {msg.subtitle}" if msg.subtitle else msg.title,
        120,
    )
    body = _truncate(
        f"{msg.body}\n{msg.url}" if msg.url and msg.url not in msg.body else msg.body,
        300,
    )
    urgency = msg.urgency if msg.urgency in {"low", "normal", "critical"} else "normal"

    notify_send = which_fn("notify-send")
    if notify_send:
        ok, detail = runner(
            [
                notify_send,
                "--app-name=Rippflow",
                f"--urgency={urgency}",
                title,
                body,
            ]
        )
        if ok:
            return NotificationReceipt(True, "linux:notify-send", "linux", detail)

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
                title,
                body,
                "[]",
                "{}",
                "8000",
            ]
        )
        if ok:
            return NotificationReceipt(True, "linux:gdbus", "linux", detail)

    kdialog = which_fn("kdialog")
    if kdialog:
        ok, detail = runner([kdialog, "--title", title, "--passivepopup", body, "8"])
        if ok:
            return NotificationReceipt(True, "linux:kdialog", "linux", detail)

    return None


def _build_windows_toast_powershell(msg: NotificationMessage) -> str:
    t1 = xml_escape(_truncate(msg.title, 80))
    t2 = xml_escape(_truncate(msg.subtitle, 100))
    body_text = f"{msg.body} {msg.url}".strip() if msg.url else msg.body
    t3 = xml_escape(_truncate(body_text, 200))
    toast_xml = (
        "<toast>"
        "<visual><binding template=\"ToastGeneric\">"
        f"<text>{t1}</text>"
        f"<text>{t2}</text>"
        f"<text>{t3}</text>"
        "</binding></visual>"
        "</toast>"
    )
    return (
        "$ErrorActionPreference = 'Stop'; "
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null; "
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
        f"$xml.LoadXml({_powershell_single_quote(toast_xml)}); "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Rippflow').Show($toast)"
    )


def _build_windows_balloon_powershell(msg: NotificationMessage) -> str:
    title = _powershell_single_quote(_truncate(f"{msg.title}: {msg.subtitle}".strip(": "), 60))
    body_text = f"{msg.body} {msg.url}".strip() if msg.url else msg.body
    body = _powershell_single_quote(_truncate(body_text, 200))
    return (
        "$ErrorActionPreference = 'Stop'; "
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Warning; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip(7000, {title}, {body}, [System.Windows.Forms.ToolTipIcon]::Warning); "
        "Start-Sleep -Milliseconds 1200; "
        "$n.Dispose()"
    )


def _send_windows(
    msg: NotificationMessage,
    *,
    which_fn=shutil.which,
    runner=_run_cmd,
) -> NotificationReceipt | None:
    ps = which_fn("pwsh") or which_fn("powershell") or which_fn("powershell.exe")
    if not ps:
        return None

    ok, detail = runner([ps, "-NoProfile", "-NonInteractive", "-Command", _build_windows_toast_powershell(msg)], 8.0)
    if ok:
        return NotificationReceipt(True, "windows:toast", "win32", detail)

    ok, detail = runner([ps, "-NoProfile", "-NonInteractive", "-Command", _build_windows_balloon_powershell(msg)], 8.0)
    if ok:
        return NotificationReceipt(True, "windows:balloon", "win32", detail)

    return None


def send_notification(
    msg: NotificationMessage,
    *,
    platform_override: str | None = None,
    alert_log_path: Path | None = None,
    enable_os_popup: bool = True,
    which_fn=shutil.which,
    runner=_run_cmd,
) -> NotificationReceipt:
    """Delivers a desktop notification on macOS, Linux, or Windows with safe fallbacks."""
    plat = (platform_override or sys.platform).lower()

    if alert_log_path is not None:
        alert_log_path.parent.mkdir(parents=True, exist_ok=True)
        with alert_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"platform": plat, **asdict(msg)}, ensure_ascii=False) + "\n")

    if not enable_os_popup:
        return NotificationReceipt(True, "disabled:log-only", plat, "OS popup suppressed by config")

    receipt: NotificationReceipt | None = None
    if plat.startswith("darwin"):
        receipt = _send_macos(msg, which_fn=which_fn, runner=runner)
    elif plat.startswith("linux"):
        receipt = _send_linux(msg, which_fn=which_fn, runner=runner)
    elif plat.startswith("win") or plat in {"cygwin", "msys"}:
        receipt = _send_windows(msg, which_fn=which_fn, runner=runner)

    if receipt is not None:
        return receipt

    # Universal terminal OSC-9 fallback (iTerm2, WezTerm, Ghostty, Windows Terminal, Kitty)
    summary = _truncate(f"{msg.title} — {msg.subtitle}: {msg.body}", 220)
    try:
        tty_path = "CONOUT$" if plat.startswith("win") else "/dev/tty"
        with open(tty_path, "w", encoding="utf-8") as tty:
            tty.write(f"\x1b]9;{summary}\x07")
            tty.flush()
        return NotificationReceipt(True, "terminal:osc9", plat, summary)
    except Exception as exc:
        print(f"[NOTIFY] {summary}", file=sys.stderr)
        return NotificationReceipt(False, "stderr:fallback", plat, str(exc))
