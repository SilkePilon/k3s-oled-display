#!/usr/bin/env python3
"""
k3s Kubernetes Cluster Stats — OLED Display
============================================
Raspberry Pi + SSD1306 128×64 OLED

Cycles through 5 info screens every SCREEN_DURATION seconds.
Cluster data is refreshed every FETCH_INTERVAL seconds via the Kubernetes API.

Screens
-------
  1. Overview      — nodes / pods / namespaces / services at a glance
  2. Pod Status    — running / pending / failed with progress bars
  3. Node Detail   — per-node info, cycles through all nodes
  4. Deployments   — ready/total with progress bar
  5. Health        — pass/fail checklist

Requirements
------------
  pip install -r requirements.txt
  Fonts (bundled in repository — no action needed):
    • PixelOperator.ttf   (CC0 1.0)
    • lineawesome-webfont.ttf  (MIT / Line Awesome solid)
  Kubeconfig at path set by KUBECONFIG env-var  (default: ./k3s-config.yaml)
  Copy k3s-config.example.yaml → k3s-config.yaml and fill in your credentials.
"""

import time
import sys
import os
from datetime import datetime

import board
import gpiozero
from PIL import Image, ImageDraw, ImageFont
import adafruit_ssd1306
from kubernetes import client, config
from kubernetes.client.rest import ApiException

# ── Configuration ─────────────────────────────────────────────────────────────

_SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))

KUBECONFIG_PATH = os.environ.get(
    "KUBECONFIG",
    os.path.join(_SCRIPT_DIR, "k3s-config.yaml"),
)
FONT_PATH       = os.path.join(_SCRIPT_DIR, "PixelOperator.ttf")
ICON_FONT_PATH  = os.path.join(_SCRIPT_DIR, "lineawesome-webfont.ttf")

SCREEN_DURATION = 10.0    # seconds each screen is displayed
FETCH_INTERVAL  = 30.0    # seconds between Kubernetes API refreshes
OLED_ADDR       = 0x3C
OLED_ROTATION   = int(os.environ.get("OLED_ROTATION", "1"))
WIDTH, HEIGHT   = 128, 64

# ── OLED Initialisation ───────────────────────────────────────────────────────

_oled_reset = gpiozero.OutputDevice(4, active_high=False)
_i2c        = board.I2C()

# Hardware reset pulse
_oled_reset.on();  time.sleep(0.1)
_oled_reset.off(); time.sleep(0.1)
_oled_reset.on()

oled = adafruit_ssd1306.SSD1306_I2C(WIDTH, HEIGHT, _i2c, addr=OLED_ADDR)

if OLED_ROTATION == 2:
    try:
        oled.rotate(2)
    except AttributeError:
        oled.rotation = 2

oled.fill(0)
oled.show()

_image = Image.new("1", (WIDTH, HEIGHT))
_draw  = ImageDraw.Draw(_image)

# ── Fonts ─────────────────────────────────────────────────────────────────────

def _load_fonts() -> dict:
    try:
        return {
            "lg":   ImageFont.truetype(FONT_PATH, 16),
            "md":   ImageFont.truetype(FONT_PATH, 12),
            "sm":   ImageFont.truetype(FONT_PATH,  8),
            "icon": ImageFont.truetype(ICON_FONT_PATH, 14),
        }
    except OSError as exc:
        print(f"[warn] font load failed ({exc}), using built-in default", file=sys.stderr)
        fallback = ImageFont.load_default()
        return {"lg": fallback, "md": fallback, "sm": fallback, "icon": fallback}

F = _load_fonts()

# ── Icon glyphs — Line Awesome / Font Awesome codepoints ─────────────────────
# Same font as the original stats script (lineawesome-webfont.ttf).

I_SERVER = chr(0xF233)   # fa-server
I_CUBE   = chr(0xF1B2)   # fa-cube        (pod)
I_HEART  = chr(0xF004)   # fa-heart       (health)
I_ROCKET = chr(0xF135)   # fa-rocket      (deployments)
I_CLOUD  = chr(0xF0C2)   # fa-cloud       (namespace)
I_WIFI   = chr(0xF1EB)   # fa-wifi        (services / network)
I_CPU    = chr(0xF2DB)   # fa-microchip
I_MEM    = chr(0xF538)   # fa-memory
I_CHECK  = chr(0xF058)   # fa-check-circle
I_WARN   = chr(0xF071)   # fa-exclamation-triangle
I_CLOCK  = chr(0xF017)   # fa-clock-o
I_DASH   = chr(0xF0E4)   # fa-tachometer  (dashboard / overview)

# ── Drawing Primitives ────────────────────────────────────────────────────────

def _clear() -> None:
    _draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill=0)

def _flush() -> None:
    oled.image(_image)
    oled.show()

def _text(x: int, y: int, s: str, size: str = "sm", fill: int = 255) -> None:
    _draw.text((x, y), s, font=F[size], fill=fill)

def _icon(x: int, y: int, glyph: str, fill: int = 255) -> None:
    _draw.text((x, y), glyph, font=F["icon"], fill=fill)

def _header(title: str, ico: str = None) -> None:
    """Inverted 13-pixel header bar across the full width."""
    _draw.rectangle((0, 0, WIDTH - 1, 13), fill=255)
    if ico:
        _draw.text((1, 0),  ico,   font=F["icon"], fill=0)
        _draw.text((17, 1), title, font=F["md"],   fill=0)
    else:
        bbox = F["md"].getbbox(title)
        tw   = bbox[2] - bbox[0]
        _draw.text(((WIDTH - tw) // 2, 1), title, font=F["md"], fill=0)

def _pbar(x: int, y: int, w: int, h: int, pct: int) -> None:
    """Outlined progress bar. pct = 0–100."""
    pct = max(0, min(100, pct))
    _draw.rectangle((x, y, x + w - 1, y + h - 1), outline=255, fill=0)
    filled = int((w - 2) * pct / 100)
    if filled > 0:
        _draw.rectangle((x + 1, y + 1, x + filled, y + h - 2), fill=255)

def _dot(x: int, y: int, ok: bool) -> None:
    """8×8 filled (ok) or outlined (not-ok) status dot."""
    _draw.ellipse((x, y, x + 8, y + 8), outline=255, fill=(255 if ok else 0))

# ── Kubernetes Data Fetching ──────────────────────────────────────────────────

def _load_kube_config() -> None:
    config.load_kube_config(config_file=KUBECONFIG_PATH)

def fetch_cluster_data() -> dict:
    """Query the Kubernetes API for cluster metrics.

    Returns a dict with keys:
      nodes, pods, ns_count, svc_count, deps, error, updated_at
    """
    data: dict = {
        "nodes":      [],
        "pods":       {"running": 0, "pending": 0, "failed": 0, "succeeded": 0, "total": 0},
        "ns_count":   0,
        "svc_count":  0,
        "deps":       {"ready": 0, "total": 0},
        "error":      None,
        "updated_at": datetime.now().strftime("%H:%M"),
    }
    try:
        core = client.CoreV1Api()
        apps = client.AppsV1Api()

        # ── Nodes ──────────────────────────────────────────────────────────
        for n in core.list_node().items:
            conds   = {c.type: c.status for c in n.status.conditions}
            ready   = conds.get("Ready", "False") == "True"
            mem_raw = n.status.capacity.get("memory", "0Ki")
            try:
                mem_mb = int(mem_raw.replace("Ki", "")) // 1024
            except ValueError:
                mem_mb = 0
            data["nodes"].append({
                "name":   n.metadata.name[:14],
                "ready":  ready,
                "cpu":    n.status.capacity.get("cpu", "?"),
                "mem_mb": mem_mb,
            })

        # ── Pods ───────────────────────────────────────────────────────────
        for p in core.list_pod_for_all_namespaces().items:
            phase = p.status.phase or "Unknown"
            data["pods"]["total"] += 1
            if   phase == "Running":   data["pods"]["running"]   += 1
            elif phase == "Pending":   data["pods"]["pending"]   += 1
            elif phase == "Succeeded": data["pods"]["succeeded"] += 1
            else:                      data["pods"]["failed"]    += 1

        # ── Namespaces ─────────────────────────────────────────────────────
        data["ns_count"] = len(core.list_namespace().items)

        # ── Services ───────────────────────────────────────────────────────
        data["svc_count"] = len(core.list_service_for_all_namespaces().items)

        # ── Deployments ────────────────────────────────────────────────────
        for dep in apps.list_deployment_for_all_namespaces().items:
            data["deps"]["total"] += 1
            desired = dep.spec.replicas or 0
            ready   = dep.status.ready_replicas or 0
            if desired > 0 and ready >= desired:
                data["deps"]["ready"] += 1

    except (ApiException, Exception) as exc:
        data["error"] = str(exc)[:48]

    return data

# ── Screen Renderers ──────────────────────────────────────────────────────────

def _screen_splash() -> None:
    _clear()
    _header("k3s DISPLAY")
    _text(14, 17, "Kubernetes", "lg")
    _text(26, 35, "Cluster", "md")
    _text(18, 50, "Starting...", "md")


def _screen_error(msg: str) -> None:
    _clear()
    _header("ERROR")
    lines = [msg[i:i + 16] for i in range(0, len(msg), 16)]
    for i, line in enumerate(lines[:3]):
        _text(2, 16 + i * 16, line, "md")


def _screen_overview(d: dict) -> None:
    _clear()
    _header("OVERVIEW")

    nr = sum(1 for n in d["nodes"] if n["ready"])
    nt = len(d["nodes"])
    pr = d["pods"]["running"]
    pt = d["pods"]["total"]

    _text(2, 15, f"Nodes:  {nr} / {nt}", "md")
    _text(2, 29, f"Pods:   {pr} / {pt}", "md")
    _text(2, 43, f"NS: {d['ns_count']}   Svc: {d['svc_count']}", "md")
    _text(2, 55, f"Updated {d['updated_at']}", "sm")


def _screen_pods(d: dict) -> None:
    _clear()
    _header("POD STATUS")

    p     = d["pods"]
    total = max(p["total"], 1)

    _text(2, 15, f"Run:  {p['running']}   Pend: {p['pending']}", "md")
    _pbar(2, 29, 124, 8, p["running"] * 100 // total)
    _text(2, 40, f"Fail: {p['failed']}   Succ: {p['succeeded']}", "md")
    _text(2, 55, f"Total: {p['total']}", "sm")


def _screen_node(d: dict, idx: int) -> None:
    _clear()
    nodes = d["nodes"]

    if not nodes:
        _header("NODES")
        _text(4, 24, "No nodes found", "md")
        return

    n  = nodes[idx % len(nodes)]
    pg = f"NODE {idx % len(nodes) + 1}/{len(nodes)}"
    _header(pg)

    status = "Ready" if n["ready"] else "NotReady"
    _text(2, 15, n["name"][:16], "md")
    _text(2, 29, f"Status: {status}", "md")
    _text(2, 43, f"CPU:  {n['cpu']} cores", "md")
    _text(2, 55, f"RAM:  {n['mem_mb'] / 1024:.1f} GB", "sm")


def _screen_deployments(d: dict) -> None:
    _clear()
    _header("DEPLOYMENTS")

    deps  = d["deps"]
    total = max(deps["total"], 1)
    pct   = deps["ready"] * 100 // total

    _text(4, 15, f"{deps['ready']} / {deps['total']}  Ready", "lg")
    _pbar(2, 34, 124, 10, pct)
    _text(2, 47, f"{pct}% healthy", "md")
    _text(2, 55, f"Svc: {d['svc_count']}   NS: {d['ns_count']}", "sm")


def _screen_health(d: dict) -> None:
    _clear()
    _header("HEALTH")

    nr   = sum(1 for n in d["nodes"] if n["ready"])
    nt   = len(d["nodes"])
    deps = d["deps"]

    def _row(y: int, label: str, ok: bool, detail: str) -> None:
        mark = "OK" if ok else "!!"
        _text(2, y, f"[{mark}] {label:<8}{detail}", "md")

    _row(15, "Nodes",  nr == nt,                   f"{nr}/{nt}")
    _row(29, "Pods",   d["pods"]["failed"] == 0,   f"{d['pods']['failed']} err")
    _row(43, "Deploy", deps["ready"] == deps["total"],
         f"{deps['ready']}/{deps['total']}")

    _text(2, 55, f"Updated {d['updated_at']}", "sm")

# ── Screen Sequencer ──────────────────────────────────────────────────────────

SCREENS = ["overview", "pods", "node", "deployments", "health"]

def _render(name: str, data: dict, node_idx: int) -> int:
    """Render the named screen. Returns (possibly advanced) node_idx."""
    if   name == "overview":     _screen_overview(data)
    elif name == "pods":         _screen_pods(data)
    elif name == "node":
        _screen_node(data, node_idx)
        if data["nodes"]:
            node_idx = (node_idx + 1) % len(data["nodes"])
    elif name == "deployments":  _screen_deployments(data)
    elif name == "health":       _screen_health(data)
    return node_idx

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Splash
    _screen_splash()
    _flush()
    time.sleep(2.0)

    # Connect to cluster
    try:
        _load_kube_config()
    except Exception as exc:
        _screen_error(f"Kubeconfig error: {exc}")
        _flush()
        time.sleep(5)
        sys.exit(1)

    data:       dict  = {}
    last_fetch: float = 0.0
    screen_idx: int   = 0
    node_idx:   int   = 0

    while True:
        now = time.monotonic()

        # Refresh cluster data when due
        if now - last_fetch >= FETCH_INTERVAL or not data:
            data       = fetch_cluster_data()
            last_fetch = now

        # Render current screen
        if data.get("error"):
            _screen_error(data["error"])
        else:
            node_idx = _render(SCREENS[screen_idx % len(SCREENS)], data, node_idx)

        _flush()
        screen_idx = (screen_idx + 1) % len(SCREENS)
        time.sleep(SCREEN_DURATION)


if __name__ == "__main__":
    main()
