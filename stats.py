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
# Sizes follow the luma.oled convention: one "hero" size for key numbers,
# a readable body size, and a compact size for titles/footers.

def _load_fonts() -> dict:
    try:
        return {
            "xl":   ImageFont.truetype(FONT_PATH, 24),
            "lg":   ImageFont.truetype(FONT_PATH, 16),
            "md":   ImageFont.truetype(FONT_PATH, 12),
            "sm":   ImageFont.truetype(FONT_PATH,  8),
        }
    except OSError as exc:
        print(f"[warn] font load failed ({exc}), using built-in default", file=sys.stderr)
        fallback = ImageFont.load_default()
        return {"xl": fallback, "lg": fallback, "md": fallback, "sm": fallback}

F = _load_fonts()

# ── Drawing Primitives ────────────────────────────────────────────────────────

def _clear() -> None:
    _draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill=0)

def _flush() -> None:
    oled.image(_image)
    oled.show()

def _text(x: int, y: int, s: str, size: str = "sm", fill: int = 255) -> None:
    _draw.text((x, y), s, font=F[size], fill=fill)

def _ctext(y: int, s: str, size: str = "md") -> None:
    """Horizontally centred text."""
    bb = F[size].getbbox(s)
    tw = bb[2] - bb[0]
    _draw.text(((WIDTH - tw) // 2, y), s, font=F[size], fill=255)

def _rtext(y: int, s: str, size: str = "md") -> None:
    """Right-aligned text, flush with the inner border edge."""
    bb = F[size].getbbox(s)
    tw = bb[2] - bb[0]
    _draw.text((WIDTH - 3 - tw, y), s, font=F[size], fill=255)

def _row(y: int, label: str, value: str, lsize: str = "md", rsize: str = "md") -> None:
    """Left label + right-aligned value spanning the full usable width."""
    _draw.text((3, y), label, font=F[lsize], fill=255)
    bb = F[rsize].getbbox(value)
    tw = bb[2] - bb[0]
    _draw.text((WIDTH - 3 - tw, y), value, font=F[rsize], fill=255)

def _frame(title: str, timestamp: str = "") -> None:
    """luma.oled-style chrome: outer border + title row + divider line.

    Layout (y coords):
      0-15  title row  (md, 12 px)
      15    divider line
      17+   content area
    """
    _draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=255)
    _draw.text((3, 2), title, font=F["md"], fill=255)
    if timestamp:
        bb = F["md"].getbbox(timestamp)
        tw = bb[2] - bb[0]
        _draw.text((WIDTH - 3 - tw, 2), timestamp, font=F["md"], fill=255)
    _draw.line([(1, 15), (WIDTH - 2, 15)], fill=255, width=1)

def _pbar(x: int, y: int, w: int, h: int, pct: int) -> None:
    """Outlined progress bar filling left-to-right. pct = 0–100."""
    pct = max(0, min(100, pct))
    _draw.rectangle((x, y, x + w - 1, y + h - 1), outline=255, fill=0)
    filled = int((w - 2) * pct / 100)
    if filled > 0:
        _draw.rectangle((x + 1, y + 1, x + filled, y + h - 2), fill=255)

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
# Layout grid (all screens share this structure):
#   y= 0      outer border top
#   y= 2      title text (md, 12 px)  |  timestamp right-aligned
#   y=15      horizontal divider
#   y=17      content line 1  (md, 12 px)
#   y=28      content line 2
#   y=39      content line 3
#   y=50      content line 4  /  progress bar
#   y=63      outer border bottom

def _screen_splash() -> None:
    _clear()
    _frame("k3s CLUSTER")
    _ctext(19, "Kubernetes", "lg")
    _ctext(38, "Loading...", "md")


def _screen_error(msg: str) -> None:
    _clear()
    _frame("ERROR")
    lines = [msg[i:i + 18] for i in range(0, len(msg), 18)]
    for i, line in enumerate(lines[:3]):
        _text(3, 17 + i * 14, line, "md")


def _screen_overview(d: dict) -> None:
    """4-row overview: Nodes / Pods / Namespaces / Services.
    Values right-aligned so numbers line up on the right edge.
    """
    _clear()
    nr = sum(1 for n in d["nodes"] if n["ready"])
    nt = len(d["nodes"])
    _frame("OVERVIEW", d["updated_at"])
    _row(17, "Nodes",      f"{nr} / {nt}")
    _row(28, "Pods",       f"{d['pods']['running']} / {d['pods']['total']}")
    _row(39, "Namespaces", str(d["ns_count"]))
    _row(50, "Services",   str(d["svc_count"]))


def _screen_pods(d: dict) -> None:
    """Running / Pending / Failed counts + a full-width progress bar."""
    _clear()
    p     = d["pods"]
    total = max(p["total"], 1)
    _frame("PODS", d["updated_at"])
    _row(17, "Running", str(p["running"]))
    _row(28, "Pending", str(p["pending"]))
    _row(39, "Failed",  str(p["failed"]))
    _pbar(3, 52, WIDTH - 6, 10, p["running"] * 100 // total)


def _screen_node(d: dict, idx: int) -> None:
    """Node name large + status right, then CPU / RAM below a divider."""
    _clear()
    nodes = d["nodes"]
    if not nodes:
        _frame("NODES")
        _ctext(32, "No nodes found", "md")
        return
    n      = nodes[idx % len(nodes)]
    num    = f"{idx % len(nodes) + 1}/{len(nodes)}"
    _frame(f"NODE {num}", d["updated_at"])
    _draw.text((3, 17), n["name"][:14], font=F["lg"], fill=255)
    status = "READY" if n["ready"] else "DOWN"
    bb     = F["md"].getbbox(status)
    _draw.text((WIDTH - 3 - (bb[2] - bb[0]), 21), status, font=F["md"], fill=255)
    _draw.line([(1, 35), (WIDTH - 2, 35)], fill=255, width=1)
    _row(38, "CPU", f"{n['cpu']} cores")
    _row(51, "RAM", f"{n['mem_mb'] / 1024:.1f} GB")


def _screen_deployments(d: dict) -> None:
    """Hero '6 / 7' number centred in xl font, then progress bar + percentage."""
    _clear()
    deps  = d["deps"]
    total = max(deps["total"], 1)
    pct   = deps["ready"] * 100 // total
    _frame("DEPLOYMENTS", d["updated_at"])
    _ctext(16, f"{deps['ready']} / {deps['total']}", "xl")  # 24 px hero
    _pbar(3, 41, WIDTH - 6, 9, pct)
    _ctext(51, f"{pct}%  healthy", "md")


def _screen_health(d: dict) -> None:
    """OK / !! status marker + label + right-aligned detail for each check."""
    _clear()
    nr   = sum(1 for n in d["nodes"] if n["ready"])
    nt   = len(d["nodes"])
    deps = d["deps"]
    _frame("HEALTH", d["updated_at"])

    def _hrow(y: int, label: str, ok: bool, detail: str) -> None:
        mark = "OK" if ok else "!!"
        _draw.text((3,  y), mark,  font=F["md"], fill=255)
        _draw.text((26, y), label, font=F["md"], fill=255)
        bb = F["md"].getbbox(detail)
        _draw.text((WIDTH - 3 - (bb[2] - bb[0]), y), detail, font=F["md"], fill=255)

    _hrow(17, "Nodes",       nr == nt,                      f"{nr}/{nt}")
    _hrow(28, "Pods",        d["pods"]["failed"] == 0,      f"{d['pods']['failed']} err")
    _hrow(39, "Deployments", deps["ready"] == deps["total"],
          f"{deps['ready']}/{deps['total']}")

# ── Screen Sequencer ──────────────────────────────────────────────────────────

def _build_screen_list(nodes: list) -> list:
    """Build the rotation, inserting one screen per discovered node.

    Example with 3 nodes:
      overview → pods → node-0 → node-1 → node-2 → deployments → health
    """
    screens: list = ["overview", "pods"]
    for i in range(max(len(nodes), 1)):
        screens.append(("node", i))
    screens += ["deployments", "health"]
    return screens


def _render(screen, data: dict) -> None:
    """Render one screen. screen is a str or a ("node", idx) tuple."""
    if screen == "overview":
        _screen_overview(data)
    elif screen == "pods":
        _screen_pods(data)
    elif isinstance(screen, tuple) and screen[0] == "node":
        _screen_node(data, screen[1])
    elif screen == "deployments":
        _screen_deployments(data)
    elif screen == "health":
        _screen_health(data)

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
    screens:    list  = _build_screen_list([])

    while True:
        now = time.monotonic()

        # Refresh cluster data when due; also rebuild the screen list so
        # newly added/removed nodes are picked up automatically.
        if now - last_fetch >= FETCH_INTERVAL or not data:
            data       = fetch_cluster_data()
            last_fetch = now
            screens    = _build_screen_list(data.get("nodes", []))

        # Render current screen
        if data.get("error"):
            _screen_error(data["error"])
        else:
            _render(screens[screen_idx % len(screens)], data)

        _flush()
        screen_idx = (screen_idx + 1) % len(screens)
        time.sleep(SCREEN_DURATION)


if __name__ == "__main__":
    main()
