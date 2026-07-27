"""OwlMyPower - automatic Windows power-plan switcher.

A system-tray utility that watches system-wide input idle time (keyboard AND
mouse, via GetLastInputInfo) and switches between an "active" and an "idle"
Windows power plan.  It also understands AC/battery state, keep-awake guards
(fullscreen apps and sustained CPU load), and per-application rules that force a
chosen plan while named processes are running.

Windows only.  Depends on: pystray, Pillow, sv-ttk, darkdetect.

Copyright 2026 RoundDarkMatter

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import ctypes
import ctypes.wintypes as wintypes  # explicit: 'import ctypes' does NOT load this
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk
except ImportError:
    print("Missing dependency. Please install: pip install pystray pillow")
    sys.exit(1)

# sv-ttk and darkdetect are imported lazily in _setup_theme (modern Windows only).
# The Windows Vista / 7 build uses the native Aero Glass look instead and does not
# require them.

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:
    print("Tkinter is required and should ship with Python on Windows.")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
__version__ = "1.0"
__author__ = "RoundDarkMatter"
APP_NAME = "OwlMyPower"
MUTEX_NAME = "OwlMyPower_SingleInstance_A9F3E1"

# Canonical Windows "Power saver" scheme GUID (locale-independent).
POWER_SAVER_GUID = "a1841308-3541-4fab-bc81-f71556f20b4a"

TICK_MS = 1000                     # main evaluation cadence (GUI thread)
ANIM_MS = 130                      # animated-logo frame cadence
TRAY_ANIM_EVERY = 3                # tray advances every N logo ticks (~2.5 fps)
CREATE_NO_WINDOW = 0x08000000      # suppress the cmd/console flash on subprocess
IDLE_ACTIVE_THRESHOLD = 1.0        # seconds of idle below which we treat as active
NOTIFY_COOLDOWN = 8.0              # min seconds between toast notifications

GUID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
UNIT_FACTORS = {"seconds": 1, "minutes": 60, "hours": 3600}

# Status pill colours (work in both light and dark themes).
COLOR_ACTIVE = "#2ecc71"
COLOR_IDLE = "#e74c3c"
COLOR_GUARD = "#f39c12"
COLOR_PAUSED = "#7f8c8d"

log = logging.getLogger(APP_NAME)


# --------------------------------------------------------------------------- #
# Win32 structures / helpers (all locale- and thread-safe, no subprocess shell)
# --------------------------------------------------------------------------- #
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_user32.GetForegroundWindow.restype = ctypes.c_void_p
_user32.MonitorFromWindow.restype = ctypes.c_void_p
_user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
_user32.GetClassNameW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
_user32.GetParent.restype = ctypes.c_void_p
_user32.GetParent.argtypes = [ctypes.c_void_p]
_kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_kernel32.CreateMutexW.restype = ctypes.c_void_p


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def get_idle_seconds() -> float:
    """Return seconds since the last system-wide keyboard OR mouse input."""
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    if not _user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    now = _kernel32.GetTickCount()
    return ((now - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def get_power_status():
    """Return (on_ac, battery_percent). Either may be None if unknown/no battery."""
    st = _SYSTEM_POWER_STATUS()
    if not _kernel32.GetSystemPowerStatus(ctypes.byref(st)):
        return None, None
    ac = st.ACLineStatus
    on_ac = True if ac == 1 else (False if ac == 0 else None)
    pct = st.BatteryLifePercent
    pct = int(pct) if pct <= 100 else None
    return on_ac, pct


def get_cpu_times():
    """Return (idle, kernel, user) 100ns tick totals, or None on failure."""
    idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    if not _kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None

    def _v(ft):
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    return _v(idle), _v(kernel), _v(user)


def is_fullscreen_foreground() -> bool:
    """True if the foreground window covers its entire monitor (movie/game)."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return False
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    if buf.value in ("Progman", "WorkerW"):  # desktop / shell
        return False
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    mon = _user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    if not _user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return False
    m = mi.rcMonitor
    return (rect.left <= m.left and rect.top <= m.top
            and rect.right >= m.right and rect.bottom >= m.bottom)


def list_process_names() -> set:
    """Return a set of lower-cased running executable names (via Toolhelp32)."""
    names = set()
    snapshot = _kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return names
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            names.add(entry.szExeFile.lower())
            ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    return names


def run_command(args):
    """Run a command with no console window, capturing output. Returns (out, code)."""
    try:
        result = subprocess.run(
            args, capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("command %s failed: %s", args, exc)
        return "", -1
    out = result.stdout.decode("oem", errors="replace") if result.stdout else ""
    return out.strip(), result.returncode


def enable_dpi_awareness():
    """Make the process per-monitor DPI aware so the UI is crisp on scaled displays."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            _user32.SetProcessDPIAware()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Windows edition detection + Aero Glass (Vista / 7)
# --------------------------------------------------------------------------- #
class _MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


def windows_version():
    """Return (major, minor) via RtlGetVersion (immune to manifest version lies)."""
    class _OSVERSIONINFOEXW(ctypes.Structure):
        _fields_ = [
            ("dwOSVersionInfoSize", ctypes.c_ulong),
            ("dwMajorVersion", ctypes.c_ulong),
            ("dwMinorVersion", ctypes.c_ulong),
            ("dwBuildNumber", ctypes.c_ulong),
            ("dwPlatformId", ctypes.c_ulong),
            ("szCSDVersion", ctypes.c_wchar * 128),
            ("wServicePackMajor", ctypes.c_ushort),
            ("wServicePackMinor", ctypes.c_ushort),
            ("wSuiteMask", ctypes.c_ushort),
            ("wProductType", ctypes.c_byte),
            ("wReserved", ctypes.c_byte),
        ]

    try:
        info = _OSVERSIONINFOEXW()
        info.dwOSVersionInfoSize = ctypes.sizeof(info)
        ctypes.windll.ntdll.RtlGetVersion(ctypes.byref(info))
        return (info.dwMajorVersion, info.dwMinorVersion)
    except Exception:
        ver = sys.getwindowsversion()
        return (ver.major, ver.minor)


def is_legacy_windows():
    """True on Windows Vista (6.0) and 7 (6.1) -> use the Aero Glass build path."""
    if os.environ.get("OWL_FORCE_LEGACY") == "1":  # test hook for the Aero UI path
        return True
    return windows_version() in ((6, 0), (6, 1))


def dwm_composition_enabled():
    """True if the Desktop Window Manager (Aero) is compositing (glass available)."""
    try:
        enabled = wintypes.BOOL()
        ctypes.windll.dwmapi.DwmIsCompositionEnabled(ctypes.byref(enabled))
        return bool(enabled.value)
    except Exception:
        return False


def enable_glass_sheet(hwnd, top=None):
    """Extend the DWM glass frame into the client area (top band, or whole window)."""
    try:
        dwm = ctypes.windll.dwmapi
        dwm.DwmExtendFrameIntoClientArea.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MARGINS)]
        dwm.DwmExtendFrameIntoClientArea.restype = ctypes.c_long
        margins = _MARGINS(-1, -1, -1, -1) if top is None else _MARGINS(0, 0, int(top), 0)
        return dwm.DwmExtendFrameIntoClientArea(ctypes.c_void_p(hwnd), ctypes.byref(margins)) == 0
    except Exception:
        return False


def acquire_single_instance():
    """Return a mutex handle if we are the only instance, else None."""
    handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if _kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


# --------------------------------------------------------------------------- #
# Power plans
# --------------------------------------------------------------------------- #
@dataclass
class PowerPlan:
    guid: str
    name: str


def get_all_power_plans():
    """Parse `powercfg /list` locale-independently (GUID by regex, name in parens)."""
    out, code = run_command(["powercfg", "/list"])
    plans = []
    if code != 0:
        return plans
    for line in out.splitlines():
        match = GUID_RE.search(line)
        if not match:
            continue
        guid = match.group(0)
        name = ""
        if "(" in line and ")" in line:
            name = line[line.find("(") + 1:line.rfind(")")].strip()
        plans.append(PowerPlan(guid=guid, name=name or guid[:8]))
    return plans


def get_active_power_plan():
    """Return the currently active plan (locale-independent)."""
    out, code = run_command(["powercfg", "/getactivescheme"])
    match = GUID_RE.search(out)
    if code != 0 or not match:
        return PowerPlan(guid="", name="Unknown")
    guid = match.group(0)
    name = out[out.find("(") + 1:out.rfind(")")].strip() if "(" in out and ")" in out else "Active"
    return PowerPlan(guid=guid, name=name)


def find_low_power_plan(plans):
    """Find a sensible idle plan: canonical Power-saver GUID, else keyword match."""
    for plan in plans:
        if plan.guid.lower() == POWER_SAVER_GUID:
            return plan
    keywords = ("power saver", "energy saver", "poupança", "économie",
                "energiespar", "risparmio", "ahorro", "低电耗", "low")
    for plan in plans:
        if any(k in plan.name.lower() for k in keywords):
            return plan
    return None


# --------------------------------------------------------------------------- #
# Paths (writable data dir + bundled resources)
# --------------------------------------------------------------------------- #
def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def data_dir() -> Path:
    """A writable directory for settings + logs (falls back to %LOCALAPPDATA%)."""
    base = _base_dir()
    try:
        probe = base / ".owl_write_test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return base
    except OSError:
        target = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
        target.mkdir(parents=True, exist_ok=True)
        return target


def resource_dir() -> Path:
    """Directory of bundled read-only assets (owl_logo.*)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent))


def settings_path() -> Path:
    return data_dir() / "owlmypower_settings.json"


def log_path() -> Path:
    return data_dir() / "owlmypower.log"


# --------------------------------------------------------------------------- #
# Animated owl logo (sleeps with "Z z Z" when idle, stirs a little when active)
# --------------------------------------------------------------------------- #
_FONT_CACHE = {}


def _zzz_font(size):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    font = None
    for name in ("segoeuib.ttf", "segoeui.ttf", "arialbd.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(name, size)
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None
    _FONT_CACHE[size] = font
    return font


def _draw_zzz(frame, phase, origin):
    """Draw three tilted 'Z' glyphs drifting up on a curved path, fading in/out."""
    ox, oy = origin
    for k in range(3):
        p = ((k / 3.0) + phase) % 1.0
        alpha = max(0, min(255, int(255 * math.sin(math.pi * p) ** 0.8)))
        if alpha <= 8:
            continue
        size = 10 + int(12 * p)
        font = _zzz_font(size)
        if font is None:
            continue
        x = ox + int(8 + 24 * p + 3 * math.sin(4 * math.pi * p))
        y = oy - int(26 * p)
        tile_px = int(size * 2.2)
        tile = Image.new("RGBA", (tile_px, tile_px), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((tile_px // 4, tile_px // 6), "Z",
                                  font=font, fill=(130, 175, 240, alpha))
        tile = tile.rotate(-14 + 10 * math.sin(2 * math.pi * p + k),
                           resample=Image.BICUBIC, expand=False)
        frame.alpha_composite(tile, (x, y))


# Eye geometry measured from owl_logo.png (normalised to image size): two
# circles centred at (0.419, 0.388) and (0.581, 0.388), radius ~0.083.
_EYES = ((0.419, 0.388), (0.581, 0.388))
_EYE_R = 0.083
_BODY = (11, 28, 43, 255)


def _with_eyelids(base, closed_frac, lash=False):
    """Copy of the hi-res owl with body-coloured eyelids drawn `closed_frac` down."""
    im = base.copy()
    if closed_frac <= 0:
        return im
    w, h = im.size
    r = int(_EYE_R * w)
    for exf, eyf in _EYES:
        cx, cy = int(exf * w), int(eyf * h)
        mask = Image.new("L", (2 * r, 2 * r), 0)
        md = ImageDraw.Draw(mask)
        md.ellipse((0, 0, 2 * r - 1, 2 * r - 1), fill=255)
        cover = int(2 * r * closed_frac)
        if cover < 2 * r:
            md.rectangle((0, cover, 2 * r, 2 * r), fill=0)
        lid = Image.new("RGBA", (2 * r, 2 * r), _BODY)
        im.paste(lid, (cx - r, cy - r), mask)
        if lash:  # soft curved line suggesting the closed eye
            d = ImageDraw.Draw(im)
            d.arc((cx - int(r * 0.62), cy - int(r * 0.55),
                   cx + int(r * 0.62), cy + int(r * 0.72)),
                  start=25, end=155, fill=(185, 200, 222, 255),
                  width=max(2, w // 140))
    return im


def _owl_with_glow(owl):
    """Return the owl composited over a soft light halo (for dark backgrounds)."""
    pad = 10
    canvas = Image.new("RGBA", (owl.width + 2 * pad, owl.height + 2 * pad), (0, 0, 0, 0))
    silhouette = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    tinted = Image.new("RGBA", owl.size, (235, 240, 250, 255))
    silhouette.paste(tinted, (pad, pad), owl)
    glow = silhouette.filter(ImageFilter.GaussianBlur(5))
    # strengthen the halo, then cap its opacity so it stays subtle on light themes
    alpha = glow.getchannel("A").point(lambda a: min(150, int(a * 1.8)))
    glow.putalpha(alpha)
    canvas.alpha_composite(glow)
    canvas.alpha_composite(owl.convert("RGBA"), (pad, pad))
    return canvas


def render_logo_frames(logo_image):
    """Return (awake_frames, sleep_frames) as lists of RGBA PIL images.

    Awake: bouncy bob with squash-and-stretch, plus a periodic eye blink.
    Sleep: eyes closed (lash line), slow breathing anchored at the feet, a
    sleepy head tilt, and tilted Z z Z drifting up from the head.
    """
    base = logo_image.convert("RGBA")
    owl_px, cw, ch = 66, 100, 104

    # Pre-render eyelid variants at source resolution for clean downscaling.
    owl_open = _owl_with_glow(base.resize((owl_px, owl_px)))
    owl_half = _owl_with_glow(_with_eyelids(base, 0.55).resize((owl_px, owl_px)))
    owl_shut = _owl_with_glow(_with_eyelids(base, 1.0).resize((owl_px, owl_px)))
    owl_sleepy = _owl_with_glow(
        _with_eyelids(base, 1.0, lash=True).resize((owl_px, owl_px))
    ).rotate(7, resample=Image.BICUBIC, expand=False)

    ow, oh = owl_open.size
    ox = (cw - ow) // 2
    oy = ch - oh - 6

    def squash(img, sy):
        """Vertical squash/stretch anchored at the bottom-centre (width compensates)."""
        sx = 1.0 - 0.45 * (sy - 1.0)
        nw, nh = max(1, int(round(img.width * sx))), max(1, int(round(img.height * sy)))
        return img.resize((nw, nh)), nw, nh

    # blink script per 16-frame loop: open except a quick half/shut/half flutter
    blink = {11: owl_half, 12: owl_shut, 13: owl_half}

    awake = []
    n = 16
    for i in range(n):
        bob = 2 * math.pi * i / (n / 2)            # two bobs per loop
        sy = 1.0 + 0.035 * math.sin(bob + math.pi)  # squash at the bottom of the bob
        body, nw, nh = squash(blink.get(i, owl_open), sy)
        dx = int(round(1.5 * math.sin(2 * math.pi * i / n + math.pi / 2)))
        dy = int(round(3.0 * math.sin(bob)))
        frame = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        frame.alpha_composite(body, (ox + dx + (ow - nw) // 2, oy + dy + (oh - nh)))
        awake.append(frame)

    sleep = []
    m = 16
    for i in range(m):
        ph = 2 * math.pi * i / m
        sy = 1.0 + 0.04 * math.sin(ph)             # slow, deep breathing
        body, nw, nh = squash(owl_sleepy, sy)
        frame = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        frame.alpha_composite(body, (ox + (ow - nw) // 2, oy + (oh - nh)))
        _draw_zzz(frame, i / float(m), (ox + ow - 34, oy + 10))
        sleep.append(frame)

    return awake, sleep


def render_tray_frames(logo_image):
    """Compact 64px tray animations with minimal motion (tray renders ~16-24px).

    The source logo has large transparent margins, so frames crop to the owl's
    content box and scale it to fill the icon; otherwise the tray owl looks tiny.
    Awake: static crisp owl with a quick periodic blink.
    Sleep: tilted closed-eye owl with one chunky Z pulsing beside the head.
    """
    base = logo_image.convert("RGBA")
    bbox = base.getbbox() or (0, 0, base.width, base.height)
    size = 64

    def body(closed=0.0, lash=False):
        im = _with_eyelids(base, closed, lash) if closed > 0 else base.copy()
        im = im.crop(bbox)
        scale = (size - 2) / max(im.width, im.height)   # fill, 1px breathing room
        return im.resize((max(1, int(im.width * scale)),
                          max(1, int(im.height * scale))), Image.LANCZOS)

    def canvas_with(img):
        if img.width > size or img.height > size:       # centre-crop oversize
            left = max(0, (img.width - size) // 2)
            top = max(0, (img.height - size) // 2)
            img = img.crop((left, top, left + min(size, img.width),
                            top + min(size, img.height)))
        frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        frame.alpha_composite(img, ((size - img.width) // 2, (size - img.height) // 2))
        return frame

    owl_open = canvas_with(body())
    owl_half = canvas_with(body(0.55))
    owl_shut = canvas_with(body(1.0))

    # ~10-frame loop at ~2.5 fps: blink flutter every ~4 s, no positional motion
    awake = [owl_open] * 8 + [owl_half, owl_shut]

    sleepy = canvas_with(body(1.0, lash=True)
                         .rotate(8, resample=Image.BICUBIC, expand=True))

    sleep = []
    z_states = ((30, 210), (24, 240), (19, 255), (24, 160))  # (font px, alpha) pulse
    for fs, alpha in z_states:
        frame = sleepy.copy()
        font = _zzz_font(fs)
        if font is not None:
            tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text((size - fs - 2, 0), "Z",
                                      font=font, fill=(140, 185, 250, alpha))
            frame.alpha_composite(tile.rotate(-12, resample=Image.BICUBIC, expand=False))
        sleep.append(frame)
    return awake, sleep


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
class OwlMyPower:
    """The tray application: detection loop, power switching, and the GUI."""

    def __init__(self, start_minimized: bool = False):
        # ----- power-plan discovery -----
        self.plans = get_all_power_plans()
        self.original_plan = get_active_power_plan()
        if self.original_plan.guid and not self._plan_by_guid(self.original_plan.guid):
            self.plans.insert(0, self.original_plan)

        if self.original_plan.guid:
            self.active_plan = self.original_plan
        else:
            self.active_plan = self.plans[0] if self.plans else PowerPlan("", "None")
        self.idle_plan = find_low_power_plan(self.plans) or self.active_plan
        self.current_plan = self.original_plan if self.original_plan.guid else self.active_plan

        # ----- settings (defaults, then overridden by disk) -----
        self.idle_value = 5
        self.idle_unit = "minutes"
        self.idle_seconds = 300
        self.start_minimized = start_minimized
        self.restore_on_exit = True
        self.notifications = True
        self.logging_enabled = False
        self.guard_fullscreen = True
        self.guard_cpu = False
        self.cpu_threshold = 25
        self.battery_saver_threshold = 0     # 0 = disabled
        self.ac_disable_idle = False
        self.app_rules = []                  # list of {"process": str, "plan_guid": str}
        self.load_settings()

        # ----- runtime state -----
        self.paused = False
        self._pause_until = None
        self.idle_elapsed = 0.0
        self._cpu_pct = 0.0
        self._prev_cpu = None
        self._last_reason = "startup"
        self._last_notify = 0.0
        self._monotonic = 0.0
        self._running = True
        self._tick_id = None
        self.icon = None
        self.logo_label = None
        self._anim_frame = 0
        self._logo_frames_awake = None
        self._logo_frames_sleep = None
        self._tray_frames_awake = None
        self._tray_frames_sleep = None
        self._tray_frame = 0
        self._tray_tick = 0
        self._tray_sleeping = None

        self._setup_logging()
        log.info("%s %s starting", APP_NAME, __version__)

        # ----- theme / edition detection -----
        self._sv_ttk = None
        self._darkdetect = None
        self.modern = not is_legacy_windows()
        self.aero_variant = "Windows Vista" if windows_version() == (6, 0) else "Windows 7"
        self._glass_header_h = 88
        self._glass_ok = (not self.modern) and dwm_composition_enabled()
        self._accent_style = "Accent.TButton"
        self._switch_style = "Switch.TCheckbutton"
        self.is_dark = False

        # ----- GUI + tray -----
        self.root = self._build_gui()
        self._build_tray()
        self._start_theme_listener()

        if self.start_minimized:
            self.root.after(0, self.hide_window)
        self._animate_logo()
        self.tick()

    # ------------------------------------------------------------------ #
    # Settings persistence
    # ------------------------------------------------------------------ #
    def _plan_by_guid(self, guid):
        if not guid:
            return None
        guid = guid.lower()
        return next((p for p in self.plans if p.guid.lower() == guid), None)

    def load_settings(self):
        try:
            with open(settings_path(), "r", encoding="utf-8-sig") as handle:  # tolerate BOM
                data = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return

        def as_int(key, current, lo=None, hi=None):
            value = data.get(key, current)
            try:
                value = int(value)
            except (TypeError, ValueError):
                return current
            if lo is not None and value < lo:
                return current
            if hi is not None and value > hi:
                return current
            return value

        def as_bool(key, current):
            value = data.get(key, current)
            return bool(value) if isinstance(value, (bool, int)) else current

        def as_str(key, current):
            value = data.get(key, current)
            return value if isinstance(value, str) else current

        self.idle_unit = as_str("idle_unit", self.idle_unit)
        if self.idle_unit not in UNIT_FACTORS:
            self.idle_unit = "seconds"
        factor = UNIT_FACTORS[self.idle_unit]
        if "idle_value" in data:
            self.idle_value = as_int("idle_value", self.idle_value, 1)
        elif "idle_seconds" in data:
            self.idle_value = max(1, as_int("idle_seconds", 300, 1) // factor)
        self.idle_seconds = max(1, self.idle_value * factor)

        self.start_minimized = as_bool("start_minimized", self.start_minimized)
        self.restore_on_exit = as_bool("restore_on_exit", self.restore_on_exit)
        self.notifications = as_bool("notifications", self.notifications)
        self.logging_enabled = as_bool("logging_enabled", self.logging_enabled)
        self.guard_fullscreen = as_bool("guard_fullscreen", self.guard_fullscreen)
        self.guard_cpu = as_bool("guard_cpu", self.guard_cpu)
        self.cpu_threshold = as_int("cpu_threshold", self.cpu_threshold, 1, 100)
        self.battery_saver_threshold = as_int("battery_saver_threshold", self.battery_saver_threshold, 0, 100)
        self.ac_disable_idle = as_bool("ac_disable_idle", self.ac_disable_idle)

        active = self._plan_by_guid(data.get("active_plan_guid") or data.get("default_plan_guid"))
        if active:
            self.active_plan = active
        idle = self._plan_by_guid(data.get("idle_plan_guid") or data.get("low_power_plan_guid"))
        if idle:
            self.idle_plan = idle

        rules = data.get("app_rules")
        if isinstance(rules, list):
            clean = []
            for rule in rules:
                if (isinstance(rule, dict) and isinstance(rule.get("process"), str)
                        and isinstance(rule.get("plan_guid"), str)):
                    clean.append({"process": rule["process"].lower(), "plan_guid": rule["plan_guid"]})
            self.app_rules = clean

    def save_settings(self):
        payload = {
            "version": __version__,
            "idle_value": self.idle_value,
            "idle_unit": self.idle_unit,
            "idle_seconds": self.idle_seconds,
            "active_plan_guid": self.active_plan.guid,
            "idle_plan_guid": self.idle_plan.guid,
            "start_minimized": self.start_minimized,
            "restore_on_exit": self.restore_on_exit,
            "notifications": self.notifications,
            "logging_enabled": self.logging_enabled,
            "guard_fullscreen": self.guard_fullscreen,
            "guard_cpu": self.guard_cpu,
            "cpu_threshold": self.cpu_threshold,
            "battery_saver_threshold": self.battery_saver_threshold,
            "ac_disable_idle": self.ac_disable_idle,
            "app_rules": self.app_rules,
        }
        try:
            path = settings_path()
            tmp = path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("could not save settings: %s", exc)

    def _setup_logging(self):
        for handler in list(log.handlers):
            log.removeHandler(handler)
        if not self.logging_enabled:
            log.addHandler(logging.NullHandler())
            return
        try:
            handler = RotatingFileHandler(
                log_path(), maxBytes=256 * 1024, backupCount=2, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            log.addHandler(handler)
            log.setLevel(logging.INFO)
        except OSError:
            log.addHandler(logging.NullHandler())

    # ------------------------------------------------------------------ #
    # GUI
    # ------------------------------------------------------------------ #
    def _build_gui(self):
        root = tk.Tk()
        root.title(f"{APP_NAME} {__version__}")
        root.minsize(560, 540)
        self._setup_theme(root)

        try:
            self.logo_image = Image.open(resource_dir() / "owl_logo.png")
            self.logo_photo = ImageTk.PhotoImage(self.logo_image.resize((56, 56)))
        except Exception:
            self.logo_image = None
            self.logo_photo = None
        self._build_logo_frames()
        try:
            root.iconbitmap(str(resource_dir() / "owl_logo.ico"))
        except Exception:
            pass

        root.update_idletasks()  # realise the native window so DWM has an HWND
        if self._glass_ok:
            self._apply_header_glass(root)

        if self.modern:
            body = ttk.Frame(root, padding=16)
            body.pack(fill=tk.BOTH, expand=True)
            self._build_header(body)
        else:
            self._build_header(root)              # flush Aero glass band at the top
            body = ttk.Frame(root, padding=16)
            body.pack(fill=tk.BOTH, expand=True)

        self._build_status_card(body)
        self._build_plans_card(body)
        self._build_timing_card(body)
        self._build_action_row(body)
        self._build_footer(body)

        self._center_window(root, 560, 560)
        root.protocol("WM_DELETE_WINDOW", self.hide_window)
        return root

    def _build_header(self, parent):
        if self.modern:
            header = ttk.Frame(parent)
            header.pack(fill=tk.X, pady=(0, 12))
            if self._logo_frames_awake:
                self.logo_label = ttk.Label(header, image=self._logo_frames_awake[0])
                self.logo_label.pack(side=tk.LEFT, padx=(0, 12))
            elif self.logo_photo:
                ttk.Label(header, image=self.logo_photo).pack(side=tk.LEFT, padx=(0, 12))
            titles = ttk.Frame(header)
            titles.pack(side=tk.LEFT, anchor="w")
            ttk.Label(titles, text=APP_NAME, font=("Segoe UI", 16, "bold")).pack(anchor="w")
            ttk.Label(titles, text="Automatic power-plan switcher",
                      font=("Segoe UI", 9)).pack(anchor="w")
            return

        # Windows Vista / 7: Aero Glass caption band. In a DWM-extended frame,
        # pixels painted pure black are composited as translucent glass, so the
        # OS renders its own native Aero glass (Vista glass on Vista, 7 on 7).
        bg = "black" if self._glass_ok else "SystemButtonFace"
        fg = "white" if self._glass_ok else "black"
        header = tk.Frame(parent, height=self._glass_header_h, bg=bg)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        inner = tk.Frame(header, bg=bg)
        inner.pack(side=tk.LEFT, padx=16, pady=12)
        if self._logo_frames_awake:
            self.logo_label = tk.Label(inner, image=self._logo_frames_awake[0], bg=bg)
            self.logo_label.pack(side=tk.LEFT, padx=(0, 12))
        elif self.logo_photo:
            tk.Label(inner, image=self.logo_photo, bg=bg).pack(side=tk.LEFT, padx=(0, 12))
        titles = tk.Frame(inner, bg=bg)
        titles.pack(side=tk.LEFT, anchor="w")
        tk.Label(titles, text=APP_NAME, font=("Segoe UI", 16, "bold"),
                 bg=bg, fg=fg).pack(anchor="w")
        tk.Label(titles, text=f"Legacy edition · {self.aero_variant}",
                 font=("Segoe UI", 9), bg=bg, fg=fg).pack(anchor="w")

    def _build_status_card(self, parent):
        card = ttk.LabelFrame(parent, text="Status", padding=12)
        card.pack(fill=tk.X, pady=(0, 10))

        top = ttk.Frame(card)
        top.pack(fill=tk.X)
        self.status_pill = tk.Label(top, text="Starting", font=("Segoe UI", 10, "bold"),
                                    bg=COLOR_ACTIVE, fg="white", padx=14, pady=4)
        self.status_pill.pack(side=tk.LEFT)
        self.plan_value_label = ttk.Label(top, text="", font=("Segoe UI", 10))
        self.plan_value_label.pack(side=tk.LEFT, padx=(12, 0))

        self.mode_label = ttk.Label(card, text="", font=("Segoe UI", 9))
        self.mode_label.pack(anchor="w", pady=(8, 2))
        self.warn_label = ttk.Label(card, text="", font=("Segoe UI", 9), foreground=COLOR_IDLE)
        self.warn_label.pack(anchor="w")

        self.countdown_bar = ttk.Progressbar(card, orient="horizontal", mode="determinate",
                                             maximum=self.idle_seconds)
        self.countdown_bar.pack(fill=tk.X, pady=(8, 2))
        self.countdown_label = ttk.Label(card, text="", font=("Segoe UI", 9))
        self.countdown_label.pack(anchor="w")

    def _build_plans_card(self, parent):
        card = ttk.LabelFrame(parent, text="Power plans", padding=12)
        card.pack(fill=tk.X, pady=(0, 10))
        card.columnconfigure(1, weight=1)

        ttk.Label(card, text="Active plan").grid(row=0, column=0, sticky="w", pady=4, padx=(0, 10))
        self.active_combo = ttk.Combobox(card, state="readonly", values=self._plan_labels())
        self.active_combo.grid(row=0, column=1, sticky="ew", pady=4)
        self.active_combo.bind("<<ComboboxSelected>>", self._on_active_selected)

        ttk.Label(card, text="Idle plan").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 10))
        self.idle_combo = ttk.Combobox(card, state="readonly", values=self._plan_labels())
        self.idle_combo.grid(row=1, column=1, sticky="ew", pady=4)
        self.idle_combo.bind("<<ComboboxSelected>>", self._on_idle_selected)

        self._sync_plan_combos()

    def _build_timing_card(self, parent):
        card = ttk.LabelFrame(parent, text="Idle timeout", padding=12)
        card.pack(fill=tk.X, pady=(0, 10))

        self.timer_var = tk.StringVar(value=str(self.idle_value))
        entry = ttk.Entry(card, width=8, textvariable=self.timer_var)
        entry.pack(side=tk.LEFT)
        self.unit_var = tk.StringVar(value=self.idle_unit)
        unit = ttk.Combobox(card, state="readonly", width=9, textvariable=self.unit_var,
                            values=list(UNIT_FACTORS.keys()))
        unit.pack(side=tk.LEFT, padx=(6, 6))
        ttk.Button(card, text="Apply", command=self.apply_idle_timer).pack(side=tk.LEFT)

    def _build_action_row(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=(2, 8))
        self.pause_btn = ttk.Button(row, text="Pause", command=self.toggle_pause,
                                    style=self._accent_style)
        self.pause_btn.pack(side=tk.LEFT)
        ttk.Button(row, text="Advanced Settings...",
                   command=self.open_advanced_settings).pack(side=tk.LEFT, padx=6)
        ttk.Button(row, text="Refresh", command=self.refresh_power_plans).pack(side=tk.LEFT)

    def _build_footer(self, parent):
        footer = ttk.Frame(parent)
        footer.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(footer, text="Minimize to Tray",
                   command=self.hide_window).pack(side=tk.LEFT)
        ttk.Button(footer, text="Exit", command=self.exit_program).pack(side=tk.LEFT, padx=6)
        ttk.Label(footer, text=f"v{__version__}  |  © 2026 {__author__}",
                  font=("Segoe UI", 8)).pack(side=tk.RIGHT)

    @staticmethod
    def _center_window(win, width, height):
        win.update_idletasks()
        x = (win.winfo_screenwidth() - width) // 2
        y = (win.winfo_screenheight() - height) // 2
        win.geometry(f"{width}x{height}+{x}+{y}")

    def _build_logo_frames(self):
        if not self.logo_image:
            return
        try:
            awake, sleep = render_logo_frames(self.logo_image)
            self._logo_frames_awake = [ImageTk.PhotoImage(f) for f in awake]
            self._logo_frames_sleep = [ImageTk.PhotoImage(f) for f in sleep]
        except Exception:
            self._logo_frames_awake = None
            self._logo_frames_sleep = None
        try:
            self._tray_frames_awake, self._tray_frames_sleep = \
                render_tray_frames(self.logo_image)
        except Exception:
            self._tray_frames_awake = None
            self._tray_frames_sleep = None

    def _animate_logo(self):
        if not (self._running and self.logo_label and self._logo_frames_awake):
            return
        sleeping = self._last_reason in ("idle", "low-battery", "paused")
        frames = self._logo_frames_sleep if sleeping else self._logo_frames_awake
        self._anim_frame = (self._anim_frame + 1) % len(frames)
        try:
            self.logo_label.configure(image=frames[self._anim_frame])
        except tk.TclError:
            return
        self._animate_tray(sleeping)
        self.root.after(ANIM_MS, self._animate_logo)

    def _animate_tray(self, sleeping):
        """Advance the tray icon at a gentle cadence (pystray setter is thread-safe)."""
        if not (self.icon and self._tray_frames_awake):
            return
        self._tray_tick += 1
        if sleeping != self._tray_sleeping:      # state flip: swap immediately
            self._tray_sleeping = sleeping
            self._tray_frame = 0
        elif self._tray_tick % TRAY_ANIM_EVERY:
            return
        frames = self._tray_frames_sleep if sleeping else self._tray_frames_awake
        self._tray_frame = (self._tray_frame + 1) % len(frames)
        try:
            self.icon.icon = frames[self._tray_frame]
        except Exception:
            pass

    def _plan_labels(self):
        names = [p.name for p in self.plans]
        labels = []
        for plan in self.plans:
            if names.count(plan.name) > 1:
                labels.append(f"{plan.name}  ({plan.guid[:8]})")
            else:
                labels.append(plan.name)
        return labels or ["No plans available"]

    def _sync_plan_combos(self):
        labels = self._plan_labels()
        self.active_combo["values"] = labels
        self.idle_combo["values"] = labels
        active_idx = self._plan_index(self.active_plan)
        idle_idx = self._plan_index(self.idle_plan)
        if active_idx is not None:
            self.active_combo.current(active_idx)
        if idle_idx is not None:
            self.idle_combo.current(idle_idx)

    def _plan_index(self, plan):
        for i, candidate in enumerate(self.plans):
            if candidate.guid == plan.guid:
                return i
        return None

    def _on_active_selected(self, _event=None):
        idx = self.active_combo.current()
        if 0 <= idx < len(self.plans):
            self.active_plan = self.plans[idx]
            self.save_settings()

    def _on_idle_selected(self, _event=None):
        idx = self.idle_combo.current()
        if 0 <= idx < len(self.plans):
            self.idle_plan = self.plans[idx]
            self.save_settings()

    def apply_idle_timer(self):
        try:
            value = int(self.timer_var.get())
            if value <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Invalid timeout", "Enter a positive whole number.")
            self.timer_var.set(str(self.idle_value))
            return
        self.idle_value = value
        self.idle_unit = self.unit_var.get()
        self.idle_seconds = value * UNIT_FACTORS[self.idle_unit]
        self.countdown_bar.configure(maximum=self.idle_seconds)
        self.save_settings()
        messagebox.showinfo("Idle timeout", f"Idle timeout set to {value} {self.idle_unit}.")

    def refresh_power_plans(self):
        self.plans = get_all_power_plans()
        active = self._plan_by_guid(self.active_plan.guid)
        idle = self._plan_by_guid(self.idle_plan.guid)
        current = self._plan_by_guid(self.current_plan.guid)
        if active:
            self.active_plan = active
        if idle:
            self.idle_plan = idle
        if current:
            self.current_plan = current
        self._sync_plan_combos()

    # ------------------------------------------------------------------ #
    # Theme
    # ------------------------------------------------------------------ #
    def _setup_theme(self, root):
        """Pick the look: sv-ttk (Windows 8.1-11) or native Aero Glass (Vista/7)."""
        style = ttk.Style()
        if self.modern:
            try:
                import sv_ttk
                import darkdetect
                self._sv_ttk = sv_ttk
                self._darkdetect = darkdetect
                self.is_dark = darkdetect.isDark() is True
                sv_ttk.set_theme("dark" if self.is_dark else "light")
                self._accent_style = "Accent.TButton"
                self._switch_style = "Switch.TCheckbutton"
                return
            except Exception:
                self._sv_ttk = None
                self._darkdetect = None

        # Legacy (or sv-ttk unavailable): native Windows / Aero widget theme.
        self.is_dark = False
        for name in ("vista", "xpnative", "winnative", "clam"):
            if name in style.theme_names():
                try:
                    style.theme_use(name)
                    break
                except tk.TclError:
                    continue
        self._accent_style = "TButton"
        self._switch_style = "TCheckbutton"

    def _apply_header_glass(self, root):
        try:
            hwnd = _user32.GetParent(root.winfo_id())
            if not enable_glass_sheet(hwnd, top=self._glass_header_h):
                self._glass_ok = False
        except Exception:
            self._glass_ok = False

    def _start_theme_listener(self):
        if not (self._sv_ttk and self._darkdetect):
            return

        def callback(theme):
            try:
                self.root.after(0, lambda: self._on_os_theme_change(str(theme).lower() == "dark"))
            except Exception:
                pass

        try:
            thread = threading.Thread(target=lambda: self._darkdetect.listener(callback), daemon=True)
            thread.start()
        except Exception:
            pass

    def _on_os_theme_change(self, dark):
        self.is_dark = dark
        try:
            if self._sv_ttk:
                self._sv_ttk.set_theme("dark" if dark else "light")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Tray
    # ------------------------------------------------------------------ #
    def _build_tray(self):
        try:
            if self._tray_frames_awake:
                image = self._tray_frames_awake[0]
            else:
                image = self._compose_tray_icon(active=True)
        except Exception:
            image = self._fallback_icon(active=True)

        menu = pystray.Menu(
            pystray.MenuItem("Show Window", lambda i, it: self.root.after(0, self.show_window),
                             default=True),
            pystray.MenuItem("Pause", lambda i, it: self.root.after(0, self.toggle_pause),
                             checked=lambda it: self.paused),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Pause 30 min", lambda i, it: self.root.after(0, lambda: self.pause_for(30))),
            pystray.MenuItem("Pause 1 hour", lambda i, it: self.root.after(0, lambda: self.pause_for(60))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda i, it: self.root.after(0, self.exit_program)),
        )
        self.icon = pystray.Icon("owlmypower", image, APP_NAME, menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def _compose_tray_icon(self, active):
        """Owl logo with a coloured status dot in the corner."""
        base = (self.logo_image.convert("RGBA").resize((64, 64))
                if self.logo_image else self._fallback_icon(active))
        image = base.copy()
        draw = ImageDraw.Draw(image)
        colour = (46, 204, 113, 255) if active else (231, 76, 60, 255)
        draw.ellipse((44, 44, 62, 62), fill=colour, outline=(255, 255, 255, 255), width=2)
        return image

    @staticmethod
    def _fallback_icon(active):
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        colour = (46, 204, 113, 255) if active else (231, 76, 60, 255)
        draw.ellipse((10, 10, 54, 54), fill=colour)
        return image

    def _update_tray_icon(self):
        """Force the animated tray to re-sync its awake/asleep pose on next tick."""
        if not self.icon:
            return
        if self._tray_frames_awake:
            self._tray_sleeping = None           # next _animate_tray call re-syncs
            return
        active = self.current_plan.guid == self.active_plan.guid
        try:
            self.icon.icon = self._compose_tray_icon(active)
        except Exception:
            pass

    def _notify(self, title, message):
        if not (self.notifications and self.icon):
            return
        if (self._monotonic - self._last_notify) < NOTIFY_COOLDOWN:
            return
        self._last_notify = self._monotonic
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Pause
    # ------------------------------------------------------------------ #
    def toggle_pause(self):
        self.set_paused(not self.paused)

    def pause_for(self, minutes):
        self.set_paused(True)
        self._pause_until = self._monotonic + minutes * 60

    def set_paused(self, paused):
        self.paused = paused
        self._pause_until = None
        if self.pause_btn:
            self.pause_btn.configure(text="Resume" if paused else "Pause")
        log.info("automation %s", "paused" if paused else "resumed")

    def _auto_resume_check(self):
        if self.paused and self._pause_until and self._monotonic >= self._pause_until:
            self.set_paused(False)

    # ------------------------------------------------------------------ #
    # Detection + switching (runs on the GUI thread via after)
    # ------------------------------------------------------------------ #
    def _sample_cpu(self):
        times = get_cpu_times()
        if times is None:
            return
        if self._prev_cpu is None:
            self._prev_cpu = times
            return
        idle0, ker0, usr0 = self._prev_cpu
        idle1, ker1, usr1 = times
        self._prev_cpu = times
        total = (ker1 - ker0) + (usr1 - usr0)
        if total <= 0:
            return
        busy = total - (idle1 - idle0)
        self._cpu_pct = max(0.0, min(100.0, 100.0 * busy / total))

    def _match_app_rule(self):
        if not self.app_rules:
            return None
        running = list_process_names()
        for rule in self.app_rules:
            if rule["process"] in running:
                plan = self._plan_by_guid(rule["plan_guid"])
                if plan:
                    return plan
        return None

    def _decide(self, idle):
        """Return (target_plan_or_None, reason). None means 'leave the plan alone'."""
        if self.paused:
            return None, "paused"

        rule_plan = self._match_app_rule()
        if rule_plan:
            return rule_plan, "app-rule"

        on_ac, pct = get_power_status()
        if (not on_ac and self.battery_saver_threshold > 0
                and pct is not None and pct <= self.battery_saver_threshold):
            return self.idle_plan, "low-battery"

        if idle >= self.idle_seconds:
            if self.ac_disable_idle and on_ac:
                return self.active_plan, "ac-hold"
            if self.guard_fullscreen and is_fullscreen_foreground():
                return self.active_plan, "fullscreen-hold"
            if self.guard_cpu and self._cpu_pct >= self.cpu_threshold:
                return self.active_plan, "cpu-hold"
            return self.idle_plan, "idle"
        return self.active_plan, "active"

    def _switch(self, plan, reason):
        if not plan.guid:
            return False
        _, code = run_command(["powercfg", "/setactive", plan.guid])
        if code != 0:
            log.warning("setactive failed (%s) for %s", code, plan.name)
            return False
        previous = self.current_plan
        self.current_plan = plan
        log.info("switched %s -> %s (%s)", previous.name, plan.name, reason)
        self._update_tray_icon()
        self._notify(APP_NAME, f"Switched to {plan.name}")
        return True

    def tick(self):
        if not self._running:
            return
        self._monotonic += TICK_MS / 1000.0
        try:
            if self.guard_cpu:
                self._sample_cpu()
            self._auto_resume_check()
            self.idle_elapsed = get_idle_seconds()
            target, reason = self._decide(self.idle_elapsed)
            self._last_reason = reason
            if target and target.guid and target.guid != self.current_plan.guid:
                self._switch(target, reason)
            self._refresh_status()
        except Exception:
            log.exception("tick failed")
        self._tick_id = self.root.after(TICK_MS, self.tick)

    def _refresh_status(self):
        reason = self._last_reason
        pill_map = {
            "paused": ("Paused", COLOR_PAUSED),
            "idle": ("Idle", COLOR_IDLE),
            "low-battery": ("Idle (battery)", COLOR_IDLE),
            "active": ("Active", COLOR_ACTIVE),
            "app-rule": ("App rule", COLOR_ACTIVE),
            "ac-hold": ("Held (AC)", COLOR_GUARD),
            "fullscreen-hold": ("Held (fullscreen)", COLOR_GUARD),
            "cpu-hold": ("Held (CPU busy)", COLOR_GUARD),
            "startup": ("Active", COLOR_ACTIVE),
        }
        text, colour = pill_map.get(reason, ("Active", COLOR_ACTIVE))
        self.status_pill.configure(text=text, bg=colour)
        self.plan_value_label.configure(text=self.current_plan.name)

        mode = self._reason_detail(reason)
        self.mode_label.configure(text=mode)

        if self.idle_plan.guid == self.active_plan.guid:
            self.warn_label.configure(text="⚠ Active and Idle plans are the same — no idle switching.")
        else:
            self.warn_label.configure(text="")

        remaining = max(0, self.idle_seconds - self.idle_elapsed)
        if self.paused:
            self.countdown_bar.configure(value=0)
            self.countdown_label.configure(text="Automation paused")
        elif reason in ("idle", "low-battery"):
            self.countdown_bar.configure(value=self.idle_seconds)
            self.countdown_label.configure(text="Idle plan active")
        else:
            self.countdown_bar.configure(value=min(self.idle_seconds, self.idle_elapsed))
            self.countdown_label.configure(text=f"Idle plan in {self._fmt_time(remaining)}")

    def _reason_detail(self, reason):
        details = {
            "paused": "Automation is paused.",
            "idle": "Idle timeout reached — using idle plan.",
            "low-battery": "Battery below threshold — forcing idle plan.",
            "active": "Recent input — using active plan.",
            "app-rule": "A per-app rule is forcing this plan.",
            "ac-hold": "On AC power — idle switching disabled.",
            "fullscreen-hold": "Fullscreen app detected — staying active.",
            "cpu-hold": f"CPU busy ({self._cpu_pct:.0f}%) — staying active.",
            "startup": "Monitoring input activity.",
        }
        return details.get(reason, "Monitoring input activity.")

    @staticmethod
    def _fmt_time(seconds):
        seconds = int(seconds)
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    # ------------------------------------------------------------------ #
    # Startup (Run key)
    # ------------------------------------------------------------------ #
    def _startup_command(self):
        exe = sys.executable
        if getattr(sys, "frozen", False):
            command = f'"{exe}"'
        else:
            command = f'"{exe}" "{Path(__file__).resolve()}"'
        if self.start_minimized:
            command += " --min"
        return command

    def is_startup_enabled(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, APP_NAME)
            return True
        except OSError:
            return False

    def set_startup(self, enable):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_SET_VALUE) as key:
                if enable:
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, self._startup_command())
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except OSError:
                        pass
        except OSError as exc:
            log.warning("could not update startup entry: %s", exc)

    # ------------------------------------------------------------------ #
    # Advanced settings dialog
    # ------------------------------------------------------------------ #
    def open_advanced_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Advanced Settings")
        win.transient(self.root)
        win.grab_set()
        try:
            win.iconbitmap(str(resource_dir() / "owl_logo.ico"))
        except Exception:
            pass

        frame = ttk.Frame(win, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        # --- Startup ---
        startup = ttk.LabelFrame(frame, text="Startup", padding=10)
        startup.pack(fill=tk.X, pady=(0, 8))
        self._startup_var = tk.BooleanVar(value=self.is_startup_enabled())
        self._startmin_var = tk.BooleanVar(value=self.start_minimized)
        ttk.Checkbutton(startup, text="Start with Windows", style=self._switch_style,
                        variable=self._startup_var,
                        command=self._on_startup_changed).pack(anchor="w", pady=2)
        ttk.Checkbutton(startup, text="Start minimized to tray", style=self._switch_style,
                        variable=self._startmin_var,
                        command=self._on_startmin_changed).pack(anchor="w", pady=2)

        # --- Behaviour ---
        behaviour = ttk.LabelFrame(frame, text="Behaviour", padding=10)
        behaviour.pack(fill=tk.X, pady=(0, 8))
        self._restore_var = tk.BooleanVar(value=self.restore_on_exit)
        self._notify_var = tk.BooleanVar(value=self.notifications)
        self._log_var = tk.BooleanVar(value=self.logging_enabled)
        ttk.Checkbutton(behaviour, text="Restore original plan on exit",
                        style=self._switch_style, variable=self._restore_var,
                        command=self._on_toggle_changed).pack(anchor="w", pady=2)
        ttk.Checkbutton(behaviour, text="Show notification when the plan switches",
                        style=self._switch_style, variable=self._notify_var,
                        command=self._on_toggle_changed).pack(anchor="w", pady=2)
        ttk.Checkbutton(behaviour, text="Enable logging (owlmypower.log)",
                        style=self._switch_style, variable=self._log_var,
                        command=self._on_toggle_changed).pack(anchor="w", pady=2)

        # --- Battery ---
        battery = ttk.LabelFrame(frame, text="Battery", padding=10)
        battery.pack(fill=tk.X, pady=(0, 8))
        self._ac_var = tk.BooleanVar(value=self.ac_disable_idle)
        ttk.Checkbutton(battery, text="Never switch to idle plan while on AC power",
                        style=self._switch_style, variable=self._ac_var,
                        command=self._on_toggle_changed).pack(anchor="w", pady=2)
        batt_row = ttk.Frame(battery)
        batt_row.pack(fill=tk.X, pady=2)
        ttk.Label(batt_row, text="Force idle plan below battery %").pack(side=tk.LEFT)
        self._battthr_var = tk.StringVar(value=str(self.battery_saver_threshold))
        ttk.Entry(batt_row, width=5, textvariable=self._battthr_var).pack(side=tk.LEFT, padx=6)
        ttk.Label(batt_row, text="(0 = off)").pack(side=tk.LEFT)

        # --- Keep-awake guards ---
        guards = ttk.LabelFrame(frame, text="Keep-awake guards", padding=10)
        guards.pack(fill=tk.X, pady=(0, 8))
        self._fs_var = tk.BooleanVar(value=self.guard_fullscreen)
        self._cpu_var = tk.BooleanVar(value=self.guard_cpu)
        ttk.Checkbutton(guards, text="Stay active during fullscreen apps (video / games)",
                        style=self._switch_style, variable=self._fs_var,
                        command=self._on_toggle_changed).pack(anchor="w", pady=2)
        cpu_row = ttk.Frame(guards)
        cpu_row.pack(fill=tk.X, pady=2)
        ttk.Checkbutton(cpu_row, text="Stay active while CPU above",
                        style=self._switch_style, variable=self._cpu_var,
                        command=self._on_toggle_changed).pack(side=tk.LEFT)
        self._cputhr_var = tk.StringVar(value=str(self.cpu_threshold))
        ttk.Entry(cpu_row, width=5, textvariable=self._cputhr_var).pack(side=tk.LEFT, padx=6)
        ttk.Label(cpu_row, text="%").pack(side=tk.LEFT)

        # --- Per-app rules + about ---
        bottom = ttk.Frame(frame)
        bottom.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(bottom, text="Per-App Rules...",
                   command=self.open_app_rules).pack(side=tk.LEFT)
        ttk.Button(bottom, text="About", command=self.open_about).pack(side=tk.LEFT, padx=6)
        ttk.Button(bottom, text="Close",
                   command=lambda: self._close_advanced(win)).pack(side=tk.RIGHT)

        win.update_idletasks()
        self._center_window(win, max(360, win.winfo_reqwidth()), win.winfo_reqheight())

    def _on_startup_changed(self):
        self.set_startup(self._startup_var.get())

    def _on_startmin_changed(self):
        self.start_minimized = self._startmin_var.get()
        self.save_settings()
        if self.is_startup_enabled():
            self.set_startup(True)  # rewrite command with/without --min

    def _on_toggle_changed(self):
        self.restore_on_exit = self._restore_var.get()
        self.notifications = self._notify_var.get()
        self.guard_fullscreen = self._fs_var.get()
        self.guard_cpu = self._cpu_var.get()
        self.ac_disable_idle = self._ac_var.get()
        if self._log_var.get() != self.logging_enabled:
            self.logging_enabled = self._log_var.get()
            self._setup_logging()
        self.save_settings()

    def _close_advanced(self, win):
        try:
            thr = int(self._battthr_var.get())
            if 0 <= thr <= 100:
                self.battery_saver_threshold = thr
        except ValueError:
            pass
        try:
            cpu = int(self._cputhr_var.get())
            if 1 <= cpu <= 100:
                self.cpu_threshold = cpu
        except ValueError:
            pass
        self.save_settings()
        win.destroy()

    # ------------------------------------------------------------------ #
    # Per-app rules dialog
    # ------------------------------------------------------------------ #
    def open_app_rules(self):
        win = tk.Toplevel(self.root)
        win.title("Per-App Rules")
        win.transient(self.root)
        win.grab_set()
        try:
            win.iconbitmap(str(resource_dir() / "owl_logo.ico"))
        except Exception:
            pass

        frame = ttk.Frame(win, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="Force a plan while a process is running:",
                  font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 8))

        tree = ttk.Treeview(frame, columns=("process", "plan"), show="headings", height=6)
        tree.heading("process", text="Process")
        tree.heading("plan", text="Plan")
        tree.column("process", width=180)
        tree.column("plan", width=200)
        tree.pack(fill=tk.BOTH, expand=True)

        def reload_rules():
            tree.delete(*tree.get_children())
            for rule in self.app_rules:
                plan = self._plan_by_guid(rule["plan_guid"])
                tree.insert("", "end", values=(rule["process"],
                                                plan.name if plan else rule["plan_guid"][:8]))

        reload_rules()

        add_row = ttk.Frame(frame)
        add_row.pack(fill=tk.X, pady=(10, 0))
        proc_var = tk.StringVar()
        ttk.Entry(add_row, textvariable=proc_var, width=18).pack(side=tk.LEFT)
        ttk.Label(add_row, text=".exe").pack(side=tk.LEFT, padx=(2, 8))
        plan_var = tk.StringVar()
        plan_combo = ttk.Combobox(add_row, state="readonly", width=20,
                                  values=self._plan_labels(), textvariable=plan_var)
        plan_combo.pack(side=tk.LEFT)

        def add_rule():
            name = proc_var.get().strip().lower()
            idx = plan_combo.current()
            if not name or not (0 <= idx < len(self.plans)):
                return
            if not name.endswith(".exe"):
                name += ".exe"
            self.app_rules = [r for r in self.app_rules if r["process"] != name]
            self.app_rules.append({"process": name, "plan_guid": self.plans[idx].guid})
            self.save_settings()
            proc_var.set("")
            reload_rules()

        def remove_rule():
            selection = tree.selection()
            if not selection:
                return
            process = tree.item(selection[0], "values")[0]
            self.app_rules = [r for r in self.app_rules if r["process"] != process]
            self.save_settings()
            reload_rules()

        ttk.Button(add_row, text="Add", command=add_rule).pack(side=tk.LEFT, padx=6)
        btn_row = ttk.Frame(frame)
        btn_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btn_row, text="Remove Selected", command=remove_rule).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Close", command=win.destroy).pack(side=tk.RIGHT)

        win.update_idletasks()
        self._center_window(win, max(440, win.winfo_reqwidth()), win.winfo_reqheight())

    def open_about(self):
        edition = "Modern (Windows 8.1-11)" if self.modern else f"Legacy ({self.aero_variant})"
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME} {__version__}\n"
            f"{edition} edition\n\n"
            "Automatic Windows power-plan switcher.\n"
            "Idle detection: keyboard + mouse (GetLastInputInfo).\n\n"
            f"© 2026 {__author__} — Apache License 2.0",
        )

    # ------------------------------------------------------------------ #
    # Window / lifecycle
    # ------------------------------------------------------------------ #
    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self):
        self.root.withdraw()

    def exit_program(self):
        if not self._running:
            return
        self._running = False
        log.info("%s shutting down", APP_NAME)
        if self._tick_id is not None:
            try:
                self.root.after_cancel(self._tick_id)
            except Exception:
                pass
        if (self.restore_on_exit and self.original_plan.guid
                and self.current_plan.guid != self.original_plan.guid):
            run_command(["powercfg", "/setactive", self.original_plan.guid])
            log.info("restored original plan %s", self.original_plan.name)
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Self-test (headless): validate the Win32 layer without building the GUI.
# --------------------------------------------------------------------------- #
def selftest():
    print(f"{APP_NAME} {__version__} self-test")
    plans = get_all_power_plans()
    print(f"  plans found: {len(plans)}")
    for plan in plans:
        print(f"    - {plan.guid}  {plan.name!r}")
    print(f"  active plan : {get_active_power_plan()}")
    low = find_low_power_plan(plans)
    print(f"  idle plan   : {low}")
    print(f"  idle seconds: {get_idle_seconds():.1f}")
    print(f"  power status: on_ac={get_power_status()[0]} battery={get_power_status()[1]}")
    print(f"  cpu times   : {'ok' if get_cpu_times() else 'unavailable'}")
    print(f"  fullscreen  : {is_fullscreen_foreground()}")
    print(f"  processes   : {len(list_process_names())} running")
    return 0


def main():
    if sys.platform != "win32":
        print("This tool is designed for Windows only.")
        return
    if "--selftest" in sys.argv:
        sys.exit(selftest())

    enable_dpi_awareness()

    mutex = acquire_single_instance()
    if mutex is None:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(APP_NAME, f"{APP_NAME} is already running (check the system tray).")
            root.destroy()
        except Exception:
            pass
        return

    app = OwlMyPower(start_minimized="--min" in sys.argv)
    app.root.mainloop()


if __name__ == "__main__":
    main()
