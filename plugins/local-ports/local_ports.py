#!/usr/bin/env python3
"""Rippflow Studio plugin: Local Ports & Containers drawer + dynamic UI tabs."""

import json
import re
import shutil
import subprocess
import sys


def get_listening_ports():
    ports = []
    seen = set()
    if shutil.which("lsof"):
        try:
            out = subprocess.check_output(
                ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=4,
            )
            for line in out.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 9:
                    continue
                proc = parts[0]
                pid = parts[1]
                name_field = parts[-2] if parts[-1] == "(LISTEN)" else parts[-1]
                m = re.search(r":(\d+)$", name_field)
                if not m:
                    continue
                port = int(m.group(1))
                key = (port, pid)
                if key in seen:
                    continue
                seen.add(key)
                ports.append(
                    {
                        "port": port,
                        "pid": pid,
                        "process": proc,
                        "address": name_field,
                    }
                )
        except Exception:
            pass
    ports.sort(key=lambda x: x["port"])
    return ports


def get_docker_containers():
    containers = []
    if shutil.which("docker"):
        try:
            out = subprocess.check_output(
                [
                    "docker",
                    "ps",
                    "--format",
                    "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}",
                ],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            )
            for line in out.splitlines():
                cols = line.split("\t")
                if len(cols) < 4:
                    continue
                containers.append(
                    {
                        "id": cols[0],
                        "name": cols[1],
                        "image": cols[2],
                        "status": cols[3],
                        "ports": cols[4] if len(cols) > 4 else "",
                    }
                )
        except Exception:
            pass
    return containers


def render_drawer():
    ports = get_listening_ports()
    containers = get_docker_containers()

    port_items = [
        {
            "id": f"port-{p['port']}-{p['pid']}",
            "label": f":{p['port']}  {p['process']}",
            "detail": f"PID {p['pid']} · {p['address']}",
            "badge": "TCP",
            "status": "success",
            "action": {
                "type": "open_tab",
                "view": f"plugin://local-ports/port:{p['port']}:{p['pid']}:{p['process']}",
            },
        }
        for p in ports[:40]
    ]

    container_items = [
        {
            "id": f"container-{c['id']}",
            "label": c["name"],
            "detail": f"{c['image']} · {c['status']}",
            "badge": "Docker",
            "status": "info",
            "action": {
                "type": "open_tab",
                "view": f"plugin://local-ports/container:{c['id']}:{c['name']}",
            },
        }
        for c in containers
    ]

    payload = {
        "title": "Local Ports & Containers",
        "summary": f"{len(ports)} listening · {len(containers)} containers",
        "sections": [
            {
                "title": "Dashboard",
                "items": [
                    {
                        "id": "overview",
                        "label": "Open Ports & Containers Overview",
                        "detail": "Live chart & table tab",
                        "badge": "Tab",
                        "status": "info",
                        "action": {
                            "type": "open_tab",
                            "view": "plugin://local-ports/overview",
                        },
                    }
                ],
            },
            {
                "title": f"Listening Ports ({len(ports)})",
                "items": port_items,
            },
            {
                "title": f"Docker Containers ({len(containers)})",
                "items": container_items,
            },
        ],
    }
    print(json.dumps(payload))


def render_overview_view():
    ports = get_listening_ports()
    containers = get_docker_containers()

    proc_counts = {}
    for p in ports:
        proc_counts[p["process"]] = proc_counts.get(p["process"], 0) + 1
    top_procs = sorted(proc_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]

    port_rows = [
        [str(p["port"]), p["process"], str(p["pid"]), p["address"]]
        for p in ports
    ]
    container_rows = [
        [c["name"], c["image"], c["status"], c["ports"] or "—"]
        for c in containers
    ]

    children = [
        {
            "type": "Row",
            "gap": 12,
            "children": [
                {
                    "type": "MetricCard",
                    "label": "Listening TCP Ports",
                    "value": str(len(ports)),
                    "status": "success" if ports else "info",
                },
                {
                    "type": "MetricCard",
                    "label": "Docker Containers",
                    "value": str(len(containers)),
                    "status": "info",
                },
                {
                    "type": "MetricCard",
                    "label": "Unique Processes",
                    "value": str(len(proc_counts)),
                    "status": "info",
                },
            ],
        }
    ]

    if top_procs:
        children.append(
            {
                "type": "BarChart",
                "title": "Listening Ports by Process",
                "labels": [k for k, _ in top_procs],
                "values": [float(v) for _, v in top_procs],
            }
        )

    children.append(
        {
            "type": "Table",
            "title": "Listening Ports",
            "columns": ["Port", "Process", "PID", "Address"],
            "rows": port_rows if port_rows else [["—", "No listening ports", "—", "—"]],
        }
    )

    if containers:
        children.append(
            {
                "type": "Table",
                "title": "Running Docker Containers",
                "columns": ["Name", "Image", "Status", "Ports"],
                "rows": container_rows,
            }
        )

    spec = {
        "version": "rippflow/ui-v1",
        "id": "local-ports.overview",
        "title": "Ports & Containers",
        "icon": "network",
        "layout": {
            "type": "Container",
            "gap": 16,
            "padding": 16,
            "children": children,
        },
    }
    print(json.dumps(spec))


def render_port_view(tab_id):
    parts = tab_id.split(":", 3)
    port = parts[1] if len(parts) > 1 else "?"
    pid = parts[2] if len(parts) > 2 else "?"
    proc = parts[3] if len(parts) > 3 else "process"

    rows = []
    if shutil.which("lsof") and pid.isdigit():
        try:
            out = subprocess.check_output(
                ["lsof", "-nP", "-p", pid],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=4,
            )
            for line in out.splitlines()[1:40]:
                cols = line.split(None, 8)
                if len(cols) >= 9:
                    rows.append([cols[3], cols[4], cols[7], cols[8]])
        except Exception:
            pass

    spec = {
        "version": "rippflow/ui-v1",
        "id": f"local-ports.port.{port}",
        "title": f"Port :{port} ({proc})",
        "icon": "network",
        "layout": {
            "type": "Container",
            "gap": 16,
            "padding": 16,
            "children": [
                {
                    "type": "Row",
                    "gap": 12,
                    "children": [
                        {
                            "type": "MetricCard",
                            "label": "Port",
                            "value": f":{port}",
                            "status": "success",
                        },
                        {
                            "type": "MetricCard",
                            "label": "Process",
                            "value": proc,
                            "status": "info",
                        },
                        {
                            "type": "MetricCard",
                            "label": "PID",
                            "value": pid,
                            "status": "info",
                        },
                    ],
                },
                {
                    "type": "Table",
                    "title": f"Open Descriptors for PID {pid}",
                    "columns": ["FD", "Type", "Node", "Name"],
                    "rows": rows if rows else [["—", "—", "—", "No open descriptors captured"]],
                },
            ],
        },
    }
    print(json.dumps(spec))


def render_container_view(tab_id):
    parts = tab_id.split(":", 2)
    cid = parts[1] if len(parts) > 1 else "?"
    cname = parts[2] if len(parts) > 2 else cid

    log_rows = []
    if shutil.which("docker") and cid != "?":
        try:
            out = subprocess.check_output(
                ["docker", "logs", "--tail", "30", cid],
                stderr=subprocess.STDOUT,
                text=True,
                timeout=4,
            )
            for i, line in enumerate(out.splitlines(), 1):
                log_rows.append([str(i), line[:200]])
        except Exception as exc:
            log_rows.append(["!", str(exc)])

    spec = {
        "version": "rippflow/ui-v1",
        "id": f"local-ports.container.{cname}",
        "title": f"Container: {cname}",
        "icon": "cpu",
        "layout": {
            "type": "Container",
            "gap": 16,
            "padding": 16,
            "children": [
                {
                    "type": "Row",
                    "gap": 12,
                    "children": [
                        {
                            "type": "MetricCard",
                            "label": "Container",
                            "value": cname,
                            "status": "success",
                        },
                        {
                            "type": "MetricCard",
                            "label": "Container ID",
                            "value": cid,
                            "status": "info",
                        },
                    ],
                },
                {
                    "type": "Table",
                    "title": f"Recent Logs ({cname})",
                    "columns": ["Line", "Message"],
                    "rows": log_rows if log_rows else [["—", "No logs available"]],
                },
            ],
        },
    }
    print(json.dumps(spec))


def main():
    if "--drawer" in sys.argv:
        render_drawer()
        return

    raw = sys.stdin.read().strip()
    req = json.loads(raw) if raw else {}
    tab_id = req.get("tab") or "overview"
    if tab_id.startswith("port:"):
        render_port_view(tab_id)
    elif tab_id.startswith("container:"):
        render_container_view(tab_id)
    else:
        render_overview_view()


if __name__ == "__main__":
    main()
