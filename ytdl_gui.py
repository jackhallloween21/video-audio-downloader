#!/usr/bin/env python3
"""
YT Downloader
=============
A modern desktop video/audio downloader built on yt-dlp + ffmpeg.
Both tools are downloaded automatically on first run (into ~/.ytdl_gui/bin).

Run from source:   python ytdl_gui.py      (Windows: pythonw ytdl_gui.py = no console)
Build an .exe:     see README.md / .github/workflows/build.yml
"""

import colorsys
import io
import json
import math
import os
import platform
import queue
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)


# --------------------------------------------------------------------------- #
# Bootstrap Python dependencies when running from source
# --------------------------------------------------------------------------- #
def _ensure(pip_name, module):
    try:
        __import__(module)
        return
    except ImportError:
        pass
    for extra in ([], ["--user"], ["--user", "--break-system-packages"]):
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", pip_name, *extra])
            __import__(module)
            return
        except Exception:
            continue
    sys.exit(f"Missing dependency '{pip_name}'. Run:  pip install -r requirements.txt")


if not FROZEN:
    _ensure("customtkinter", "customtkinter")
    _ensure("pillow", "PIL")

import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageOps

# --------------------------------------------------------------------------- #
# Constants / paths
# --------------------------------------------------------------------------- #
APP_NAME = "YT Downloader"
SYSTEM = platform.system()
MACHINE = platform.machine().lower()
IS_WIN = SYSTEM == "Windows"
IS_MAC = SYSTEM == "Darwin"
IS_ARM = "aarch64" in MACHINE or "arm64" in MACHINE
EXE = ".exe" if IS_WIN else ""

APP_DIR = Path.home() / ".ytdl_gui"
BIN_DIR = APP_DIR / "bin"
THUMB_DIR = APP_DIR / "thumbs"
SETTINGS_FILE = APP_DIR / "settings.json"
HISTORY_FILE = APP_DIR / "history.json"
for _d in (BIN_DIR, THUMB_DIR):
    _d.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (YT-Downloader)"}

# Theme ---------------------------------------------------------------------------
PALETTES = {
    "dark": {
        "bg": "#121417", "card": "#1c1f24", "card2": "#262a31", "hover": "#30353d",
        "accent": "#FFD23F", "accent_h": "#F2BF1B", "text": "#E8EAED", "muted": "#8B929C",
        "green": "#4CD08A", "red": "#FF6B6B", "seg_on": "#3B4250", "seg_on_h": "#465063",
        "on_accent": "#1a1a1a",
    },
    "light": {
        "bg": "#EEF1F5", "card": "#FFFFFF", "card2": "#E2E7ED", "hover": "#D3D9E1",
        "accent": "#F0B90B", "accent_h": "#D9A700", "text": "#1B1F26", "muted": "#69727E",
        "green": "#16A46B", "red": "#E04B4B", "seg_on": "#C9D1DB", "seg_on_h": "#B8C2CE",
        "on_accent": "#1a1a1a",
    },
}
ACCENT_PRESETS = [
    ("Default", None),
    ("Acid Lime", "#C6FF00"), ("Amethyst", "#B14AED"), ("Amber", "#FFB300"),
    ("Aquamarine", "#7FFFD4"), ("Abyss", "#1B1F24"), ("Atomic Purple", "#7C3AED"),
    ("Breaking Bad", "#3F9142"), ("Brick", "#C74B50"), ("Carbon", "#333333"),
    ("Coffee", "#6F4E37"), ("Cyan", "#00BCD4"), ("Daisy", "#FFDD55"),
    ("Dodgers Blue", "#0073E6"), ("Fuchsia", "#FF0090"), ("Graphite", "#5C5C5C"),
    ("Indigo", "#3F51B5"), ("Lavender", "#B57EDC"), ("Light", "#FFFFFF"),
    ("Lime", "#32CD32"), ("Neon", "#00E676"), ("Oceanic", "#2E5266"),
    ("Orange", "#FF8C00"), ("Palenight", "#546E7A"), ("Plant", "#7CB342"),
    ("Porpoise", "#577277"), ("Sky", "#38BDF8"),
]
ACCENT_OVERRIDE = {"hex": None}
_CUR_THEME = {"name": "dark"}
BG = CARD = CARD2 = HOVER = ACCENT = ACCENT_H = TEXT = MUTED = GREEN = RED = ""
SEG_ON = SEG_ON_H = ON_ACCENT = "#1a1a1a"


def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_hex(rgb):
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def _shade(h, f):
    return _rgb_hex([c * f for c in _hex_rgb(h)])


def _mix(a, b, t):
    ra, rb = _hex_rgb(a), _hex_rgb(b)
    return _rgb_hex([ra[i] * t + rb[i] * (1 - t) for i in range(3)])


def _lum(h):
    r, g, b = (c / 255 for c in _hex_rgb(h))
    r, g, b = ([c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def normalize_hex(v):
    v = str(v).strip()
    if not re.match(r"^#?[0-9a-fA-F]{6}$", v):
        return None
    return "#" + v.lstrip("#").upper()


def hsv_hex(h, s, v):
    r, g, b = colorsys.hsv_to_rgb((h % 360) / 360.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return "#%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))


def _disc_img(color, size=40):
    """Filled circle PNG with a faint dark rim so light swatches stay visible."""
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((0, 0, big - 1, big - 1), fill=color,
                                outline=(0, 0, 0, 90), width=6)
    return img


def _disc(color, size=40):
    img = _disc_img(color, size)
    return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))


_RAINBOW_CACHE = {}


def _rainbow_img(size=40):
    """Tiny HSV wheel disc used by the 'Custom' swatch."""
    if size in _RAINBOW_CACHE:
        return _RAINBOW_CACHE[size]
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    px = img.load()
    c = (big - 1) / 2.0
    for y in range(big):
        dy = y - c
        for x in range(big):
            dx = x - c
            dist = math.hypot(dx, dy)
            if dist > c:
                continue
            hue = (math.degrees(math.atan2(dy, dx)) + 90.0) % 360.0
            r, g, b = colorsys.hsv_to_rgb(hue / 360.0, min(dist / c, 1.0), 1.0)
            px[x, y] = (round(r * 255), round(g * 255), round(b * 255), 255)
    ImageDraw.Draw(img).ellipse((0, 0, big - 1, big - 1), outline=(0, 0, 0, 90), width=6)
    _RAINBOW_CACHE[size] = img
    return img


def _rainbow_disc(size=40):
    try:
        p = resource_path(os.path.join("assets", "icon-accent.png"))
        if os.path.exists(p):
            img = Image.open(p).convert("RGBA")
            return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    except Exception:
        pass
    img = _rainbow_img(size)
    return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))


_WHEEL_CACHE = {}


def _wheel_img(size=190, ss=2):
    """HSV wheel: hue by angle (0° at top, clockwise), saturation by radius."""
    key = (size, ss)
    if key not in _WHEEL_CACHE:
        big = size * ss
        img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        px = img.load()
        c = (big - 1) / 2.0
        for y in range(big):
            dy = y - c
            for x in range(big):
                dx = x - c
                dist = math.hypot(dx, dy)
                if dist > c:
                    continue
                hue = (math.degrees(math.atan2(dy, dx)) + 90.0) % 360.0
                r, g, b = colorsys.hsv_to_rgb(hue / 360.0, min(dist / c, 1.0), 1.0)
                px[x, y] = (round(r * 255), round(g * 255), round(b * 255), 255)
        _WHEEL_CACHE[key] = img.resize((size, size), Image.LANCZOS)
    return _WHEEL_CACHE[key]


def _bar_img(h, s, w=190, hgt=16):
    """Black (left) → full value (right) gradient for a hue/saturation."""
    img = Image.new("RGB", (w, hgt))
    d = ImageDraw.Draw(img)
    for x in range(w):
        r, g, b = colorsys.hsv_to_rgb((h % 360) / 360.0, max(0.0, min(1.0, s)), x / (w - 1))
        d.line((x, 0, x, hgt - 1), fill=(round(r * 255), round(g * 255), round(b * 255)))
    return img


def _derive_accent():
    global ACCENT, ACCENT_H, ON_ACCENT, SEG_ON, SEG_ON_H
    ACCENT = ACCENT_OVERRIDE["hex"] or PALETTES[_CUR_THEME["name"]]["accent"]
    ACCENT_H = _shade(ACCENT, 0.86)
    ON_ACCENT = "#1a1a1a" if _lum(ACCENT) > 0.55 else "#FFFFFF"
    SEG_ON = _mix(ACCENT, CARD, 0.40)
    SEG_ON_H = _mix(ACCENT, CARD, 0.60)


def apply_accent(hexv):
    ACCENT_OVERRIDE["hex"] = normalize_hex(hexv) if hexv else None
    _derive_accent()


def set_theme(name):
    """Point the module-level colour constants at a palette (dark / light)."""
    global BG, CARD, CARD2, HOVER, ACCENT, ACCENT_H, TEXT, MUTED, GREEN, RED
    global SEG_ON, SEG_ON_H, ON_ACCENT
    key = "light" if str(name).lower() == "light" else "dark"
    _CUR_THEME["name"] = key
    p = PALETTES[key]
    BG, CARD, CARD2, HOVER = p["bg"], p["card"], p["card2"], p["hover"]
    ACCENT, ACCENT_H, TEXT, MUTED = p["accent"], p["accent_h"], p["text"], p["muted"]
    GREEN, RED, SEG_ON, SEG_ON_H = p["green"], p["red"], p["seg_on"], p["seg_on_h"]
    ON_ACCENT = p["on_accent"]
    _derive_accent()


set_theme("dark")

QUALITY_V = {"Highest": 0, "4K · 2160p": 2160, "1440p": 1440, "1080p": 1080,
             "720p": 720, "480p": 480, "360p": 360}
QUALITY_A = {"Best": None, "320 kbps": "320K", "256 kbps": "256K",
             "192 kbps": "192K", "128 kbps": "128K"}
VIDEO_FMTS = ["MP4", "MKV", "WEBM"]
AUDIO_FMTS = ["MP3", "M4A", "OPUS", "FLAC", "WAV"]

DEFAULTS = {
    "mode": "video", "q_video": "Highest", "q_audio": "Best",
    "f_video": "MP4", "f_audio": "MP3",
    "outdir": str(Path.home() / "Downloads"),
    "parallel": 2, "embed_meta": True, "embed_thumb": False,
    "theme": "dark", "accent": None,
}

# Multi-segment downloading: aria2c splits each file into SEGMENTS ranged parts,
# FRAG_THREADS covers HLS/DASH streams that are split into fragments.
SEGMENTS = 16
FRAG_THREADS = 8


def resource_path(rel):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(base / rel)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Windows niceties: no console window, proper taskbar icon
# --------------------------------------------------------------------------- #
def hide_own_console():
    """If Windows opened a console just for this script, hide it."""
    if not IS_WIN or FROZEN:
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        hwnd = k32.GetConsoleWindow()
        if hwnd:
            procs = (ctypes.c_uint * 4)()
            if k32.GetConsoleProcessList(procs, 4) <= 1:
                ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def set_app_id():
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ytdl.gui.app")
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Subprocess helpers (never show a terminal window)
# --------------------------------------------------------------------------- #
NO_WIN = 0x08000000 if IS_WIN else 0


def _startupinfo():
    if IS_WIN:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return si
    return None


def run(cmd, **kw):
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, creationflags=NO_WIN,
                          startupinfo=_startupinfo(), **kw)


def popen(cmd, **kw):
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, creationflags=NO_WIN,
                            startupinfo=_startupinfo(), **kw)


def tool_env():
    env = os.environ.copy()
    env["PATH"] = str(BIN_DIR) + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def kill_tree(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        if IS_WIN:
            run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.terminate()
    except Exception:
        pass


def open_path(p):
    try:
        if IS_WIN:
            os.startfile(p)  # noqa
        elif IS_MAC:
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception:
        pass


def reveal(p):
    try:
        if IS_WIN:
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(p)}"])
        elif IS_MAC:
            subprocess.Popen(["open", "-R", p])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(p)])
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def fmt_size(n):
    if not n:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def to_bytes(tok):
    """'5.4MiB' / '712KiB' / '12345' -> bytes (float)."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGTPE]?)i?B?", (tok or "").strip(), re.I)
    if not m:
        return 0.0
    scale = {"": 1, "K": 1024, "M": 1048576, "G": 1073741824,
             "T": 1099511627776, "P": 1125899906842624, "E": 1152921504606846976}
    return float(m.group(1)) * scale.get(m.group(2).upper(), 1)


def to_secs(tok):
    """'45s' / '1m20s' / '2h' / '--' -> seconds (float)."""
    if not tok or tok in ("--", "-", "?"):
        return 0.0
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    found = re.findall(r"(\d+)([dhms])", tok)
    return sum(float(v) * units[u] for v, u in found) if found else 0.0


ARIA_RE = re.compile(r"#\S+\s+(\d+(?:\.\d+)?[KMGTPE]?i?B)/(\d+(?:\.\d+)?[KMGTPE]?i?B)\((\d+)%\)"
                     r"(?:[^\]]*?DL:(\d+(?:\.\d+)?[KMGTPE]?i?B))?(?:[^\]]*?ETA:([^\]\s]+))?")


def parse_progress(line):
    """Return (downloaded, total, speed, eta) for a yt-dlp or aria2c progress line."""
    if line.startswith("[PROG]"):
        parts = line.split()[1:6]
        if len(parts) < 5:
            return None
        d, t, est, sp, eta = (num(x) for x in parts)
        return d, (t or est), sp, eta
    if line.startswith("[#"):        # aria2c multi-segment readout
        m = ARIA_RE.search(line)
        if not m:
            return None
        d, tot = to_bytes(m.group(1)), to_bytes(m.group(2))
        if not tot and m.group(3):
            tot = d * 100 / int(m.group(3))
        return d, tot, to_bytes(m.group(4)), to_secs(m.group(5))
    return None


def num(x):
    return float(x) if x and re.fullmatch(r"[\d.]+(e[+-]?\d+)?", x) else 0.0


def fmt_dur(sec):
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return ""
    if sec <= 0:
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def ellipsize(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]', "_", s or "playlist").strip()[:80] or "playlist"


def to_playlist_url(u):
    # watch?v=..&list=PL.. -> playlist?list=PL.. so the whole playlist is fetched.
    # RD mixes and WL/LL/UL (private/auto lists) are skipped: they are not real playlists.
    if "youtube.com/" not in u and "youtu.be/" not in u:
        return u
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", u)
    if not m or m.group(1)[:2] in ("RD", "WL", "LL", "UL"):
        return u
    return f"https://www.youtube.com/playlist?list={m.group(1)}"


# --------------------------------------------------------------------------- #
# Auto-installer (yt-dlp, ffmpeg, deno)
# --------------------------------------------------------------------------- #
def download_file(url, dest, cb=None):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if cb and total:
                cb(done * 100 / total)


def make_exec(path):
    if not IS_WIN:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def find_tool(name):
    local = BIN_DIR / (name + EXE)
    if local.exists():
        return str(local)
    return shutil.which(name)


def ytdlp_url():
    base = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
    if IS_WIN:
        return base + "yt-dlp.exe"
    if IS_MAC:
        return base + "yt-dlp_macos"
    return base + ("yt-dlp_linux_aarch64" if IS_ARM else "yt-dlp_linux")


def install_ytdlp(cb=None):
    dest = BIN_DIR / ("yt-dlp" + EXE)
    tmp = dest.with_suffix(".tmp")
    download_file(ytdlp_url(), tmp, cb)
    if dest.exists():
        dest.unlink()
    tmp.rename(dest)
    make_exec(dest)


def _extract_zip_members(archive, wanted, in_bin_dir=False):
    with zipfile.ZipFile(archive) as z:
        for n in z.namelist():
            base = os.path.basename(n)
            if base in wanted and (not in_bin_dir or "/bin/" in n):
                with z.open(n) as src, open(BIN_DIR / base, "wb") as out:
                    shutil.copyfileobj(src, out)
                make_exec(BIN_DIR / base)


def install_ffmpeg(cb=None):
    archive = APP_DIR / "download.tmp"
    try:
        if IS_WIN:
            download_file("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip", archive, cb)
            _extract_zip_members(archive, ("ffmpeg.exe", "ffprobe.exe"), in_bin_dir=True)
        elif IS_MAC:
            for tool, url in (("ffmpeg", "https://evermeet.cx/ffmpeg/getrelease/zip"),
                              ("ffprobe", "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip")):
                download_file(url, archive, cb)
                _extract_zip_members(archive, (tool,))
        else:
            arch = "arm64" if IS_ARM else "amd64"
            download_file(f"https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-{arch}-static.tar.xz",
                          archive, cb)
            with tarfile.open(archive, "r:xz") as t:
                for m in t.getmembers():
                    base = os.path.basename(m.name)
                    if m.isfile() and base in ("ffmpeg", "ffprobe"):
                        with t.extractfile(m) as src, open(BIN_DIR / base, "wb") as out:
                            shutil.copyfileobj(src, out)
                        make_exec(BIN_DIR / base)
    finally:
        archive.unlink(missing_ok=True)
    if not (BIN_DIR / ("ffmpeg" + EXE)).exists():
        raise RuntimeError("ffmpeg extraction failed")


def install_deno(cb=None):
    """JS runtime that recent yt-dlp versions use for full YouTube support."""
    base = "https://github.com/denoland/deno/releases/latest/download/"
    if IS_WIN:
        name = "deno-x86_64-pc-windows-msvc.zip"
    elif IS_MAC:
        name = "deno-aarch64-apple-darwin.zip" if IS_ARM else "deno-x86_64-apple-darwin.zip"
    else:
        name = "deno-aarch64-unknown-linux-gnu.zip" if IS_ARM else "deno-x86_64-unknown-linux-gnu.zip"
    archive = APP_DIR / "deno.tmp"
    try:
        download_file(base + name, archive, cb)
        _extract_zip_members(archive, ("deno" + EXE,))
    finally:
        archive.unlink(missing_ok=True)


def install_aria2(cb=None):
    """aria2c - multi-segment HTTP downloader, splits every file into SEGMENTS parts."""
    if not IS_WIN:
        raise RuntimeError("aria2c is not bundled for this OS; install it from your package manager")
    url = ("https://github.com/aria2/aria2/releases/download/release-1.37.0/"
           "aria2-1.37.0-win-64bit-build1.zip")
    archive = APP_DIR / "aria2.tmp"
    try:
        download_file(url, archive, cb)
        _extract_zip_members(archive, ("aria2c" + EXE,))
    finally:
        archive.unlink(missing_ok=True)
    if not (BIN_DIR / ("aria2c" + EXE)).exists():
        raise RuntimeError("aria2c extraction failed")


# --------------------------------------------------------------------------- #
# Format selection (so the size/resolution we show is what we download)
# --------------------------------------------------------------------------- #
def _fsize(f, dur):
    s = f.get("filesize") or f.get("filesize_approx")
    if s:
        return int(s)
    tbr = f.get("tbr") or ((f.get("vbr") or 0) + (f.get("abr") or 0))
    if tbr and dur:
        return int(tbr * 125 * dur)
    return 0


def pick_formats(info, mode, cap, container):
    """Return dict(fmt, sizes, height, fps, ext, abr)."""
    fmts = info.get("formats") or []
    dur = info.get("duration") or 0
    has_v = lambda f: f.get("vcodec") not in (None, "none")
    has_a = lambda f: f.get("acodec") not in (None, "none")
    v_only = [f for f in fmts if has_v(f) and not has_a(f) and f.get("height")]
    a_only = [f for f in fmts if has_a(f) and not has_v(f)]
    combined = [f for f in fmts if has_v(f) and has_a(f) and f.get("height")]

    if mode == "audio":
        pool = a_only or [f for f in fmts if has_a(f)]
        best = max(pool, key=lambda f: (f.get("abr") or f.get("tbr") or 0), default=None)
        return {"fmt": "bestaudio/best", "sizes": [_fsize(best, dur)] if best else [0],
                "height": 0, "fps": 0, "ext": container, "abr": (best or {}).get("abr") or 0}

    def within(lst):
        ok = [f for f in lst if not cap or f["height"] <= cap]
        return ok or ([min(lst, key=lambda f: f["height"])] if lst else [])

    def compat(f):
        if container == "mp4":
            if str(f.get("vcodec", "")).startswith(("avc1", "h264")):
                return 2
            return 1 if f.get("ext") == "mp4" else 0
        if container == "webm":
            return 1 if f.get("ext") == "webm" else 0
        return 0

    key = lambda f: (f["height"], compat(f), f.get("fps") or 0, f.get("tbr") or 0)

    if v_only and a_only:
        vid = max(within(v_only), key=key)
        want = {"mp4": "m4a", "webm": "webm"}.get(container)
        aud = max(a_only, key=lambda f: (1 if f.get("ext") == want else 0, f.get("abr") or f.get("tbr") or 0))
        return {"fmt": f"{vid['format_id']}+{aud['format_id']}",
                "sizes": [_fsize(vid, dur), _fsize(aud, dur)],
                "height": vid["height"], "fps": vid.get("fps") or 0, "ext": container, "abr": 0}
    if combined:
        f = max(within(combined), key=key)
        return {"fmt": f["format_id"], "sizes": [_fsize(f, dur)], "height": f["height"],
                "fps": f.get("fps") or 0, "ext": f.get("ext") or container, "abr": 0}
    return {"fmt": "bv*+ba/b", "sizes": [0], "height": info.get("height") or 0,
            "fps": info.get("fps") or 0, "ext": container, "abr": 0}


# --------------------------------------------------------------------------- #
# Job model
# --------------------------------------------------------------------------- #
PERSIST = ("url", "mode", "fmt", "bitrate", "title", "uploader", "duration", "size", "height",
           "fps", "ext_label", "path", "thumb_path", "ts")


class Job:
    def __init__(self, url, mode, cap, fmt, bitrate, outdir):
        self.id = uuid.uuid4().hex[:10]
        self.seq = 0
        self.url, self.mode, self.cap, self.fmt, self.bitrate, self.outdir = url, mode, cap, fmt, bitrate, outdir
        self.state = "fetching"      # fetching ready queued downloading processing paused done error cancelled
        self.title, self.uploader = url, ""
        self.duration = self.size = self.height = self.fps = 0
        self.size_est = True
        self.ext_label = fmt
        self.fmt_sel = None
        self.stream_sizes = [0]
        self.thumb_path = None
        self.path = None
        self.error = ""
        self.cancel = False
        self.pause = False
        self.frac = 0.0
        self.proc = None
        self.row = None
        self.ts = time.time()

    def meta_text(self):
        if not self.fmt_sel and self.state != "done":
            return ""
        p = []
        if self.duration:
            p.append(fmt_dur(self.duration))
        if self.size:
            p.append(("≈ " if self.size_est else "") + fmt_size(self.size))
        if self.mode == "video":
            if self.ext_label:
                p.append(str(self.ext_label).upper())
            if self.height:
                p.append(f"{self.height}p" + (str(round(self.fps)) if self.fps and self.fps > 30 else ""))
        else:
            p.append(self.fmt.upper())
            if self.bitrate:
                p.append(self.bitrate.replace("K", " kbps"))
        if self.uploader:
            p.append(self.uploader)
        return "   ·   ".join(p)

    def to_dict(self):
        return {k: getattr(self, k) for k in PERSIST}

    @classmethod
    def from_dict(cls, d):
        j = cls(d.get("url", ""), d.get("mode", "video"), None, d.get("fmt", "mp4"),
                d.get("bitrate"), os.path.dirname(d.get("path") or ""))
        for k in PERSIST:
            if k in d:
                setattr(j, k, d[k])
        j.state, j.size_est = "done", False
        return j


# --------------------------------------------------------------------------- #
# UI: thumbnail helpers
# --------------------------------------------------------------------------- #
THUMB_W, THUMB_H = 168, 94


def rounded(img, radius=20):
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=radius, fill=255)
    img.putalpha(mask)
    return img


def to_ctk(pil):
    pil = rounded(pil)
    return ctk.CTkImage(light_image=pil, dark_image=pil, size=(THUMB_W, THUMB_H))


def fetch_thumb(url, cache_path):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = r.read()
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img = ImageOps.fit(img, (THUMB_W * 2, THUMB_H * 2), Image.LANCZOS)
    img.save(cache_path, "JPEG", quality=88)
    return img


# --------------------------------------------------------------------------- #
# Row widget (one download in the list)
# --------------------------------------------------------------------------- #
class Row(ctk.CTkFrame):
    def __init__(self, master, app, job):
        super().__init__(master, fg_color=CARD, corner_radius=14)
        self.app, self.job = app, job
        self._img = None
        self._bar_shown = False
        self.columnconfigure(1, weight=1)

        self.thumb = ctk.CTkLabel(self, text="", image=app.placeholder, width=THUMB_W, height=THUMB_H)
        self.thumb.grid(row=0, column=0, rowspan=3, padx=12, pady=12)

        self.title = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=480,
                                  font=ctk.CTkFont(size=14, weight="bold"), text_color=TEXT)
        self.title.grid(row=0, column=1, sticky="ew", pady=(14, 0))

        self.meta = ctk.CTkLabel(self, text="", anchor="w", text_color=MUTED, font=ctk.CTkFont(size=12))
        self.meta.grid(row=1, column=1, sticky="ew", pady=(2, 0))

        self.pf = ctk.CTkFrame(self, fg_color="transparent")
        self.pf.grid(row=2, column=1, sticky="ew", pady=(6, 12), padx=(0, 8))
        self.bar = ctk.CTkProgressBar(self.pf, height=6, progress_color=ACCENT, fg_color=BG)
        self.bar.set(0)
        self.status = ctk.CTkLabel(self.pf, text="", anchor="w", font=ctk.CTkFont(size=12), text_color=MUTED)
        self.status.pack(fill="x")

        self.actions = ctk.CTkFrame(self, fg_color="transparent")
        self.actions.grid(row=0, column=2, rowspan=3, padx=12)
        self.refresh()

    # -- helpers --
    def set_thumb(self, pil):
        self._img = to_ctk(pil)
        self.thumb.configure(image=self._img)

    def _show_bar(self, show):
        if show and not self._bar_shown:
            self.bar.pack(fill="x", pady=(0, 5), before=self.status)
            self._bar_shown = True
        elif not show and self._bar_shown:
            self.bar.pack_forget()
            self._bar_shown = False

    def set_progress(self, frac, text):
        self._show_bar(True)
        self.bar.set(max(0.0, min(frac, 1.0)))
        self.status.configure(text=text, text_color=MUTED)

    def _btn(self, text, cmd, accent=False):
        b = ctk.CTkButton(self.actions, text=text, command=cmd, width=84, height=30, corner_radius=8,
                          fg_color=ACCENT if accent else CARD2,
                          hover_color=ACCENT_H if accent else HOVER,
                          text_color=ON_ACCENT if accent else TEXT,
                          font=ctk.CTkFont(size=12, weight="bold" if accent else "normal"))
        b.pack(pady=3)

    def refresh(self):
        j, app = self.job, self.app
        self.title.configure(text=ellipsize(j.title, 90))
        self.meta.configure(text=j.meta_text())
        for w in self.actions.winfo_children():
            w.destroy()

        st = j.state
        if st == "ready":
            self._show_bar(False)
            self.status.configure(text="Ready to download", text_color=MUTED)
            self._btn("Download", lambda: app.start_job(j), accent=True)
            self._btn("Remove", lambda: app.remove_job(j))
        elif st == "fetching":
            self._show_bar(False)
            self.status.configure(text="Fetching info…", text_color=MUTED)
            self._btn("Cancel", lambda: app.cancel_job(j))
        elif st == "queued":
            self._show_bar(False)
            self.status.configure(text="Waiting in queue…", text_color=MUTED)
            self._btn("Cancel", lambda: app.cancel_job(j))
        elif st == "downloading":
            self._show_bar(True)
            self._btn("Pause", lambda: app.pause_job(j))
            self._btn("Cancel", lambda: app.cancel_job(j))
        elif st == "paused":
            self._show_bar(True)
            self.status.configure(text=f"Paused · {int((j.frac or 0) * 100)}%", text_color=MUTED)
            self._btn("Resume", lambda: app.resume_job(j), accent=True)
            self._btn("Remove", lambda: app.remove_job(j))
        elif st == "processing":
            self._show_bar(True)
            self.bar.set(1)
            self.status.configure(text="Processing with ffmpeg…", text_color=MUTED)
            self._btn("Cancel", lambda: app.cancel_job(j))
        elif st == "done":
            self._show_bar(False)
            if j.path and os.path.exists(j.path):
                self.status.configure(text="✓ Completed", text_color=GREEN)
                self._btn("Play", lambda: open_path(j.path), accent=True)
                self._btn("Show file", lambda: reveal(j.path))
            else:
                self.status.configure(text="File not found (moved or deleted)", text_color=MUTED)
            self._btn("Remove", lambda: app.remove_job(j))
        else:  # error / cancelled
            self._show_bar(False)
            if st == "cancelled":
                self.status.configure(text="Cancelled", text_color=MUTED)
            else:
                self.status.configure(text=ellipsize(j.error or "Download failed", 110), text_color=RED)
            self._btn("Retry", lambda: app.retry_job(j), accent=True)
            self._btn("Remove", lambda: app.remove_job(j))


# --------------------------------------------------------------------------- #
# Main app
# --------------------------------------------------------------------------- #
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.s = {**DEFAULTS, **load_json(SETTINGS_FILE, {})}
        if not os.path.isdir(self.s["outdir"]):
            self.s["outdir"] = DEFAULTS["outdir"]
        self.theme = "light" if str(self.s.get("theme", "dark")).lower() == "light" else "dark"
        self.s["theme"] = self.theme
        set_theme(self.theme)
        apply_accent(self.s.get("accent"))
        ctk.set_appearance_mode(self.theme)
        self.title(APP_NAME)
        self.geometry("1000x740")
        self.minsize(860, 580)
        self.configure(fg_color=BG)

        self.ui_q = queue.Queue()
        self.info_q = queue.Queue()
        self.jobs = []
        self.seq = 0
        self.ytdlp = self.ffmpeg = None
        self.ready = False
        self._flash_id = None
        self._accent_win = None
        self._acc_pane = "grid"
        self._banner_state = ("Preparing…", 0.0)
        self.placeholder = to_ctk(Image.new("RGB", (THUMB_W * 2, THUMB_H * 2), CARD2))

        self._set_icon()
        self._build()
        self._apply_mode()
        self._load_history()
        self._relayout()

        for _ in range(3):
            threading.Thread(target=self._info_loop, daemon=True).start()
        threading.Thread(target=self._setup, daemon=True).start()
        self.after(40, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- plumbing ---------------------------------------------------------- #
    def post(self, fn, *a):
        self.ui_q.put((fn, a))

    def _poll(self):
        try:
            while True:
                fn, a = self.ui_q.get_nowait()
                try:
                    fn(*a)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.after(40, self._poll)

    def save_settings(self):
        save_json(SETTINGS_FILE, self.s)

    # ---- theme -------------------------------------------------------------- #
    def toggle_theme(self):
        self.apply_theme("light" if self.theme == "dark" else "dark")

    def apply_theme(self, name):
        name = "light" if str(name).lower() == "light" else "dark"
        if name == self.theme:
            return
        self.theme = name
        self.s["theme"] = name
        self.save_settings()
        set_theme(name)
        ctk.set_appearance_mode(name)
        self._rebuild_ui()

    def _rebuild_ui(self):
        self.configure(fg_color=BG)
        self.placeholder = to_ctk(Image.new("RGB", (THUMB_W * 2, THUMB_H * 2), CARD2))
        for w in self.winfo_children():
            if isinstance(w, ctk.CTkToplevel):
                continue
            w.destroy()
        self._build()
        self._apply_mode()
        if self.ready:
            self.banner.pack_forget()
            self.btn_paste.configure(state="normal")
            self.btn_download.configure(state="normal")
        else:
            text, frac, error = self._banner_state
            self._banner(text, frac, error)
            if error:
                self.banner_retry.pack(anchor="w", padx=16, pady=(0, 12))
        for j in self.jobs:
            j.row = Row(self.listf, self, j)
            if j.thumb_path and os.path.exists(j.thumb_path):
                try:
                    j.row.set_thumb(Image.open(j.thumb_path).convert("RGB"))
                except Exception:
                    pass
        self._relayout()

    def set_accent(self, hexv):
        if hexv is not None and str(hexv).strip():
            clean = normalize_hex(hexv)
            if not clean:
                self.flash("Enter a color like #FF5733", RED)
                return False
            hexv = clean
        else:
            hexv = None
        self.s["accent"] = hexv
        self.save_settings()
        apply_accent(hexv)
        self._rebuild_ui()
        self.flash("Accent: " + (hexv or "theme default"))
        self._refresh_accent_win()
        return True

    def _refresh_accent_win(self):
        win = getattr(self, "_accent_win", None)
        if win is not None:
            try:
                if win.winfo_exists():
                    win.after(1, self._render_accent)
            except Exception:
                pass

    def _swatch(self, size=16):
        try:
            img = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
            ImageDraw.Draw(img).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=ACCENT)
            img = img.resize((size, size), Image.LANCZOS)
            return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
        except Exception:
            return None

    def open_accent(self):
        win = getattr(self, "_accent_win", None)
        if win is not None:
            try:
                if win.winfo_exists():
                    win.deiconify()
                    win.lift()
                    win.focus()
                    return
            except Exception:
                pass
        win = ctk.CTkToplevel(self)
        self._accent_win = win
        self._acc_pane = "grid"
        win.title("Accent color")
        win.geometry("470x510")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.configure(fg_color=CARD)
        self._render_accent()
        win.after(200, win.focus)

    def _render_accent(self):
        win = getattr(self, "_accent_win", None)
        if win is None or not win.winfo_exists():
            return
        for w in win.winfo_children():
            w.destroy()
        F = ctk.CTkFont
        ctk.CTkLabel(win, text="Accent color", font=F(size=16, weight="bold"),
                     text_color=TEXT, height=22).pack(padx=20, pady=(8, 0), anchor="w")
        ctk.CTkLabel(win, text="Pick a swatch · or dial your own", font=F(size=12),
                     text_color=MUTED, height=16).pack(padx=20, pady=(0, 4), anchor="w")
        grid_frame = self._accent_grid(win)
        wheel_frame = ctk.CTkFrame(win, fg_color="transparent")
        if getattr(self, "_acc_pane", "grid") == "wheel":
            self._accent_wheel(wheel_frame)
            wheel_frame.pack(pady=(2, 0))
        else:
            grid_frame.pack(pady=(2, 0))
        self._acc_frames = (grid_frame, wheel_frame)
        ctk.CTkButton(win, text="Cancel", width=100, height=26, corner_radius=10,
                      fg_color=CARD2, hover_color=HOVER, text_color=TEXT, font=F(size=13),
                      command=win.withdraw).pack(pady=(4, 4))

    def _accent_grid(self, parent):
        F = ctk.CTkFont
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        selected = ACCENT_OVERRIDE["hex"]
        default_hex = PALETTES[_CUR_THEME["name"]]["accent"]
        for i, (label, hexv) in enumerate(list(ACCENT_PRESETS) + [("Custom", "custom")]):
            custom = hexv == "custom"
            sel = hexv == selected
            cell = ctk.CTkFrame(frame, fg_color="transparent", width=70, height=76,
                                corner_radius=12, border_width=2 if sel else 0,
                                border_color=TEXT)
            cell.grid(row=i // 6, column=i % 6, padx=4, pady=4)
            cell.grid_propagate(False)
            cell.pack_propagate(False)
            name_lbl = ctk.CTkLabel(cell, text=label, wraplength=66, height=26,
                                    font=F(size=9, weight="bold" if sel else "normal"),
                                    text_color=TEXT if sel else MUTED)
            name_lbl.pack(pady=(6, 3))
            image = _rainbow_disc() if custom else _disc(default_hex if hexv is None else hexv)
            disc_lbl = ctk.CTkLabel(cell, text="", image=image, width=40, height=40)
            disc_lbl.pack()
            if custom:
                hit = lambda e: self._acc_show_wheel()
            else:
                hit = lambda e, v=hexv: self.set_accent(v)
            for w in (cell, name_lbl, disc_lbl):
                w.bind("<Button-1>", hit)
        return frame

    def _accent_wheel(self, pane):
        F = ctk.CTkFont
        h, s, v = self._wheel_state()
        hexv = hsv_hex(h, s, v)
        self._wh_wheel = self._wheel(self._wh_mark())
        self._wh_lbl = ctk.CTkLabel(pane, text="", image=self._wh_wheel, width=190, height=190)
        self._wh_lbl.pack(pady=(4, 8))
        self._wh_lbl.bind("<Button-1>", self._wheel_pick)
        self._wh_lbl.bind("<B1-Motion>", self._wheel_pick)
        self._wh_lbl.bind("<ButtonRelease-1>", self._wheel_commit)
        self._wh_bar_img = self._bar_ctk(h, s)
        self._wh_bar = ctk.CTkLabel(pane, text="", image=self._wh_bar_img, width=190, height=16)
        self._wh_bar.pack(pady=(0, 8))
        self._wh_bar.bind("<Button-1>", self._bar_pick)
        row = ctk.CTkFrame(pane, fg_color="transparent")
        row.pack(pady=(0, 8))
        self._wh_prev_img = _disc(hexv, 36)
        self._wh_prev = ctk.CTkLabel(row, text="", image=self._wh_prev_img, width=36, height=36)
        self._wh_prev.pack(side="left", padx=(0, 8))
        self._wh_entry = ctk.CTkEntry(row, width=120, height=32, corner_radius=10,
                                      fg_color=CARD2, text_color=TEXT, border_width=1,
                                      border_color=HOVER, font=F(size=13))
        self._wh_entry.pack(side="left", padx=(0, 8))
        self._wh_entry.insert(0, hexv)
        ctk.CTkButton(row, text="Apply", width=90, height=32, corner_radius=10,
                      fg_color=ACCENT, text_color=ON_ACCENT, hover_color=ACCENT_H,
                      font=F(size=13, weight="bold"),
                      command=lambda: self.set_accent(self._wh_entry.get())).pack(side="left")
        ctk.CTkButton(pane, text="← All colors", width=130, height=26, corner_radius=10,
                      fg_color=CARD2, hover_color=HOVER, text_color=TEXT, font=F(size=13),
                      command=self._acc_show_grid).pack(pady=(0, 4))

    def _acc_show_wheel(self):
        grid_frame, wheel_frame = self._acc_frames
        if not wheel_frame.winfo_children():
            self._accent_wheel(wheel_frame)
        self._acc_pane = "wheel"
        grid_frame.pack_forget()
        wheel_frame.pack(pady=(2, 0))

    def _acc_show_grid(self):
        grid_frame, wheel_frame = self._acc_frames
        self._acc_pane = "grid"
        wheel_frame.pack_forget()
        grid_frame.pack(pady=(2, 0))

    def _wheel_state(self):
        if not hasattr(self, "_wh"):
            h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in _hex_rgb(ACCENT)))
            self._wh, self._ws, self._wv = h * 360.0, s, v
        return self._wh, self._ws, self._wv

    def _wh_mark(self):
        t = math.radians(self._wh % 360.0)
        r = self._ws * 95.0
        return round(95 + math.sin(t) * r), round(95 - math.cos(t) * r)

    @staticmethod
    def _bar_ctk(h, s):
        bar = _bar_img(h, s)
        return ctk.CTkImage(light_image=bar, dark_image=bar, size=(190, 16))

    def _wheel(self, mark=None):
        """Supersampled HSV wheel PNG; mark is the (x, y) selection in 190-space."""
        img = _wheel_img().copy()
        if mark is not None:
            d = ImageDraw.Draw(img)
            x, y = mark
            d.ellipse((x - 7, y - 7, x + 7, y + 7), outline=(255, 255, 255, 255), width=4)
            d.ellipse((x - 3, y - 3, x + 3, y + 3), outline=(27, 31, 38, 255), width=2)
        return ctk.CTkImage(light_image=img, dark_image=img, size=(190, 190))

    def _wheel_update(self):
        if getattr(self, "_wh_lbl", None) is None or not self._wh_lbl.winfo_exists():
            return
        hexv = hsv_hex(self._wh, self._ws, self._wv)
        self._wh_wheel = self._wheel(self._wh_mark())
        self._wh_lbl.configure(image=self._wh_wheel)
        self._wh_bar_img = self._bar_ctk(self._wh, self._ws)
        self._wh_bar.configure(image=self._wh_bar_img)
        self._wh_prev_img = _disc(hexv, 36)
        self._wh_prev.configure(image=self._wh_prev_img)
        self._wh_entry.delete(0, "end")
        self._wh_entry.insert(0, hexv)

    def _wheel_pick(self, event):
        """Press/drag over the wheel: store hue + saturation, refresh, no commit."""
        if getattr(self, "_wh_lbl", None) is None or not self._wh_lbl.winfo_exists():
            return
        size = self._wh_lbl.winfo_reqwidth() or 190
        k = size / 190.0
        dx = (event.x - size / 2.0) / k
        dy = (event.y - size / 2.0) / k
        self._wh = (math.degrees(math.atan2(dy, dx)) + 90.0) % 360.0
        self._ws = min(math.hypot(dx, dy) / 95.0, 1.0)
        self._wheel_update()

    def _wheel_commit(self, event):
        """Release over the wheel: commit the dialled colour once."""
        self._wheel_pick(event)
        self.set_accent(hsv_hex(self._wh, self._ws, self._wv))

    def _bar_pick(self, event):
        if getattr(self, "_wh_bar", None) is None or not self._wh_bar.winfo_exists():
            return
        w = self._wh_bar.winfo_reqwidth() or 190
        self._wv = max(0.0, min(1.0, event.x / float(w)))
        self._wheel_update()

    def flash(self, msg, color=MUTED):
        self.footer.configure(text=msg, text_color=color)
        if self._flash_id:
            self.after_cancel(self._flash_id)
        self._flash_id = self.after(5000, lambda: self.footer.configure(text=""))

    def _set_icon(self):
        ico, png = resource_path("assets/icon.ico"), resource_path("assets/icon.png")
        try:
            if IS_WIN and os.path.exists(ico):
                # CustomTkinter resets the icon shortly after start-up, so set it afterwards.
                self.after(300, lambda: self.iconbitmap(ico))
            elif os.path.exists(png):
                self._icon_photo = tk.PhotoImage(file=png)
                self.iconphoto(True, self._icon_photo)
        except Exception:
            pass

    def _load_icon(self, name, size, alt=None):
        def _get_pil(fname):
            if not fname:
                return None
            base, ext = os.path.splitext(fname)
            svg_file = resource_path(os.path.join("assets", base + ".svg"))
            png_file = resource_path(os.path.join("assets", base + ".png"))
            if os.path.exists(svg_file):
                try:
                    from PySide6.QtGui import QGuiApplication, QImage, QPainter
                    from PySide6.QtSvg import QSvgRenderer
                    from PySide6.QtCore import QByteArray
                    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
                    with open(svg_file, "r", encoding="utf-8") as f:
                        svg_data = f.read()
                    if "-inv" in fname or "-inv" in base:
                        svg_data = svg_data.replace("currentColor", "#FFFFFF")
                    else:
                        svg_data = svg_data.replace("currentColor", "#1A1A1A")
                    renderer = QSvgRenderer(QByteArray(svg_data.encode("utf-8")))
                    w, h = size[0] * 4, size[1] * 4
                    qimg = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
                    qimg.fill(0)
                    p = QPainter(qimg)
                    renderer.render(p)
                    p.end()
                    ptr = qimg.constBits()
                    return Image.frombuffer("RGBA", (w, h), bytes(ptr), "raw", "BGRA", 0, 1)
                except Exception:
                    pass
            # PySide6 is unavailable in the frozen build -> fall back to the
            # shipped PNGs (tint the base art white when an -inv variant is missing).
            stem = base[:-4] if base.endswith("-inv") else base
            cands = [png_file]
            if stem != base:
                cands.append(resource_path(os.path.join("assets", stem + ".png")))
            for i, p in enumerate(cands):
                if not os.path.exists(p):
                    continue
                try:
                    im = Image.open(p).convert("RGBA")
                except Exception:
                    continue
                if i:
                    a = im.getchannel("A")
                    im = Image.merge("RGBA", (Image.new("L", im.size, 255),) * 3 + (a,))
                return im
            target_path = resource_path(os.path.join("assets", fname))
            if os.path.exists(target_path):
                try:
                    return Image.open(target_path).convert("RGBA")
                except Exception:
                    return None
            return None

        try:
            img = _get_pil(name)
            if img is None:
                return None
            dark = _get_pil(alt) if alt else img
            return ctk.CTkImage(light_image=img, dark_image=dark or img, size=size)
        except Exception:
            return None

    def _on_close(self):
        for j in self.jobs:
            kill_tree(j.proc)
        self.destroy()

    # ---- layout ------------------------------------------------------------ #
    def _build(self):
        F = ctk.CTkFont
        dl_icon_file = "icon-download-inv.svg" if ON_ACCENT == "#FFFFFF" else "icon-download.svg"
        self._ico_dl = self._load_icon(dl_icon_file, (18, 18))
        self._ico_paste = self._load_icon("icon-paste.svg", (18, 18))
        self._ico_accent = self._load_icon("icon-accent.svg", (18, 18))
        self._ico_settings = self._load_icon("icon-settings.svg", (20, 20))
        # header
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=22, pady=(16, 6))
        try:
            logo = Image.open(resource_path("assets/icon.png")).convert("RGBA")
            self._logo = ctk.CTkImage(light_image=logo, dark_image=logo, size=(38, 38))
            ctk.CTkLabel(head, text="", image=self._logo).pack(side="left", padx=(0, 10))
        except Exception:
            pass
        ctk.CTkLabel(head, text=APP_NAME, font=F(size=20, weight="bold"), text_color=TEXT).pack(side="left")
        ctk.CTkLabel(head, text="powered by yt-dlp + ffmpeg", font=F(size=12),
                     text_color=MUTED).pack(side="left", padx=12, pady=(6, 0))
        self.btn_theme = ctk.CTkButton(head, text="☀  Light" if self.theme == "dark" else "☾  Dark",
                                        width=104, height=32, corner_radius=10, fg_color=CARD2,
                                        hover_color=HOVER, text_color=TEXT, font=F(size=13),
                                        command=self.toggle_theme)
        self.btn_theme.pack(side="right", padx=(12, 0))
        self.btn_accent = ctk.CTkButton(head, text="Accent", image=self._ico_accent, compound="left",
                                        width=104, height=32, corner_radius=10, fg_color=CARD2,
                                        hover_color=HOVER, text_color=TEXT, font=F(size=13),
                                        command=self.open_accent)
        self.btn_accent.pack(side="right", padx=(0, 10))
        ctk.CTkButton(head, text="Clear completed", width=130, height=32, corner_radius=10, fg_color="transparent",
                      hover_color=CARD2, text_color=MUTED, font=F(size=13),
                      command=self.clear_completed).pack(side="right", padx=(0, 10))

        # toolbar card
        top = ctk.CTkFrame(self, fg_color=CARD, corner_radius=16)
        top.pack(fill="x", padx=20, pady=(4, 8))
        r1 = ctk.CTkFrame(top, fg_color="transparent")
        r1.pack(fill="x", padx=14, pady=(14, 8))
        self.btn_download = ctk.CTkButton(r1, text="Download", command=self.start_all, height=42, width=150,
                                          corner_radius=12, fg_color=ACCENT, hover_color=ACCENT_H,
                                          text_color=ON_ACCENT, font=F(size=14, weight="bold"), state="disabled",
                                          image=self._ico_dl, compound="left")
        self.btn_download.pack(side="left")
        self.entry = ctk.CTkEntry(r1, height=42, corner_radius=12, border_width=0, fg_color=BG,
                                  font=F(size=14),
                                  placeholder_text="…or type / paste a link, then press Enter")
        self.entry.pack(side="left", fill="x", expand=True, padx=10)
        self.entry.bind("<Return>", lambda _e: self.add_from_entry())
        self.entry.bind("<FocusIn>", lambda _e: self.entry.configure(border_width=1, border_color=ACCENT))
        self.entry.bind("<FocusOut>", lambda _e: self.entry.configure(border_width=0))
        self.btn_paste = ctk.CTkButton(r1, text="Paste", command=self.paste_link, height=42, width=120,
                                       corner_radius=12, fg_color=CARD2, hover_color=HOVER,
                                       text_color=TEXT, border_width=1, border_color=HOVER,
                                       font=F(size=14, weight="bold"), state="disabled",
                                       image=self._ico_paste, compound="left")
        self.btn_paste.pack(side="left", padx=(0, 8))
        self.btn_settings = ctk.CTkButton(r1, text="", image=self._ico_settings, width=42, height=42,
                                          corner_radius=12, fg_color=CARD2, hover_color=HOVER,
                                          command=self.open_settings)
        self.btn_settings.pack(side="left")

        r2 = ctk.CTkFrame(top, fg_color="transparent")
        r2.pack(fill="x", padx=14, pady=(0, 14))
        opt = dict(fg_color=BG, button_color=CARD2, button_hover_color=HOVER, dropdown_fg_color=CARD,
                   dropdown_hover_color=HOVER, dropdown_text_color=TEXT, text_color=TEXT,
                   corner_radius=10, height=34)

        def group(t, expand=False):
            g = ctk.CTkFrame(r2, fg_color=CARD2, corner_radius=12)
            g.pack(side="left", fill="x" if expand else "none", expand=expand, padx=(0, 12))
            ctk.CTkLabel(g, text=t, text_color=MUTED, font=F(size=11)).pack(anchor="w", padx=12, pady=(9, 2))
            return g

        g = group("Download")
        self.seg_mode = ctk.CTkSegmentedButton(g, values=["Video", "Audio"], command=self._mode_cb,
                                               selected_color=SEG_ON, selected_hover_color=SEG_ON_H,
                                               unselected_color=BG, unselected_hover_color=HOVER,
                                               fg_color=BG, text_color=TEXT, height=34)
        self.seg_mode.pack(padx=12, pady=(0, 10))
        g = group("Quality")
        self.opt_q = ctk.CTkOptionMenu(g, values=["Highest"], command=self._quality_cb, width=130, **opt)
        self.opt_q.pack(padx=12, pady=(0, 10))
        g = group("Format")
        self.opt_f = ctk.CTkOptionMenu(g, values=["MP4"], command=self._format_cb, width=100, **opt)
        self.opt_f.pack(padx=12, pady=(0, 10))
        g = group("Save to", expand=True)
        self.btn_dir = ctk.CTkButton(g, text="", command=self.choose_dir, height=34, corner_radius=10,
                                     fg_color=BG, hover_color=HOVER, text_color=TEXT, anchor="w", width=180)
        self.btn_dir.pack(fill="x", expand=True, padx=12, pady=(0, 10))
        self._update_dir_label()

        # footer + banner + list
        self.footer = ctk.CTkLabel(self, text="", anchor="w", text_color=MUTED, font=F(size=12))
        self.footer.pack(side="bottom", fill="x", padx=24, pady=(0, 8))

        self.banner = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14)
        self.banner_lbl = ctk.CTkLabel(self.banner, text="Preparing…", anchor="w", text_color=TEXT)
        self.banner_lbl.pack(fill="x", padx=16, pady=(12, 6))
        self.banner_bar = ctk.CTkProgressBar(self.banner, height=6, progress_color=ACCENT, fg_color=BG)
        self.banner_bar.set(0)
        self.banner_bar.pack(fill="x", padx=16, pady=(0, 8))
        self.banner_retry = ctk.CTkButton(self.banner, text="Retry setup", width=110, height=30,
                                          fg_color=ACCENT, hover_color=ACCENT_H, text_color=ON_ACCENT,
                                          command=self._retry_setup)
        self.banner.pack(fill="x", padx=20, pady=(0, 8))

        self.listf = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.listf.pack(fill="both", expand=True, padx=14, pady=(2, 6))
        self.empty = ctk.CTkFrame(self.listf, fg_color="transparent")
        ctk.CTkLabel(self.empty, text="Copy a video link from your browser",
                     text_color=MUTED, font=F(size=14)).pack(pady=(0, 12))
        steps = ctk.CTkFrame(self.empty, fg_color="transparent")
        steps.pack()

        def chip(text, icon):
            c = ctk.CTkFrame(steps, fg_color=CARD, corner_radius=10)
            c.pack(side="left")
            if icon is not None:
                small = ctk.CTkImage(light_image=icon.cget("light_image"),
                                     dark_image=icon.cget("dark_image"), size=(16, 16))
                ctk.CTkLabel(c, text="", image=small).pack(side="left", padx=(10, 4), pady=8)
            ctk.CTkLabel(c, text=text, text_color=TEXT,
                         font=F(size=13, weight="bold")).pack(side="left", padx=(0, 10), pady=8)

        chip("Paste Link", self._ico_paste)
        ctk.CTkLabel(steps, text="→", text_color=MUTED, font=F(size=16)).pack(side="left", padx=10)
        chip("Download", self._ico_dl)


    # ---- toolbar callbacks --------------------------------------------------- #
    def _mode_cb(self, v):
        self.s["mode"] = v.lower()
        self.save_settings()
        self._apply_mode()

    def _apply_mode(self):
        m = self.s["mode"]
        self.seg_mode.set(m.capitalize())
        qs = list(QUALITY_V if m == "video" else QUALITY_A)
        fs = VIDEO_FMTS if m == "video" else AUDIO_FMTS
        self.opt_q.configure(values=qs)
        self.opt_q.set(self.s["q_" + m] if self.s["q_" + m] in qs else qs[0])
        self.opt_f.configure(values=fs)
        self.opt_f.set(self.s["f_" + m] if self.s["f_" + m] in fs else fs[0])

    def _quality_cb(self, v):
        self.s["q_" + self.s["mode"]] = v
        self.save_settings()

    def _format_cb(self, v):
        self.s["f_" + self.s["mode"]] = v
        self.save_settings()

    def _update_dir_label(self):
        base_name = os.path.basename(self.s["outdir"].rstrip("/\\")) or self.s["outdir"]
        self.btn_dir.configure(text="  📁  " + ellipsize(base_name, 26))

    def choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.s["outdir"])
        if d:
            self.s["outdir"] = d
            self.save_settings()
            self._update_dir_label()

    # ---- settings dialog ----------------------------------------------------- #
    def open_settings(self):
        win = ctk.CTkToplevel(self)
        win.title("Settings")
        win.geometry("420x430")
        win.resizable(False, False)
        win.configure(fg_color=BG)
        win.transient(self)
        F = ctk.CTkFont
        pad = dict(padx=22, anchor="w")

        hdr = ctk.CTkFrame(win, fg_color="transparent")
        hdr.pack(fill="x", padx=22, pady=(18, 6))
        if getattr(self, "_ico_settings", None):
            ctk.CTkLabel(hdr, text="", image=self._ico_settings).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(hdr, text="Settings", font=F(size=18, weight="bold"), text_color=TEXT).pack(side="left")

        ctk.CTkLabel(win, text="Simultaneous downloads", text_color=MUTED, font=F(size=12)).pack(pady=(10, 6), **pad)
        seg = ctk.CTkSegmentedButton(win, values=["1", "2", "3", "4"], selected_color=SEG_ON,
                                     unselected_color=CARD, fg_color=CARD, text_color=TEXT,
                                     command=lambda v: self._set_setting("parallel", int(v)))
        seg.set(str(self.s["parallel"]))
        seg.pack(**pad)

        def sw(text, key):
            var = tk.BooleanVar(value=self.s[key])
            ctk.CTkSwitch(win, text=text, variable=var, progress_color=ACCENT, text_color=TEXT,
                          command=lambda: self._set_setting(key, var.get())).pack(pady=(16, 0), **pad)

        sw("Embed metadata (title, artist…)", "embed_meta")
        sw("Embed thumbnail (mp3 / m4a / flac / mp4 / mkv)", "embed_thumb")

        self.upd_btn = ctk.CTkButton(win, text="Update yt-dlp", fg_color=CARD2, hover_color=HOVER,
                                     text_color=TEXT, command=self.update_ytdlp)
        self.upd_btn.pack(pady=(22, 8), **pad)
        ctk.CTkButton(win, text="Open data folder", fg_color=CARD2, hover_color=HOVER, text_color=TEXT,
                      command=lambda: open_path(str(APP_DIR))).pack(**pad)

        ctk.CTkButton(win, text="Done", width=100, height=32, corner_radius=10,
                      fg_color=ACCENT, hover_color=ACCENT_H, text_color=ON_ACCENT,
                      font=F(size=13, weight="bold"), command=win.destroy).pack(pady=(18, 12))
        win.after(200, win.focus)

    def _set_setting(self, k, v):
        self.s[k] = v
        self.save_settings()
        if k == "parallel":
            self._pump()

    def update_ytdlp(self):
        if not self.ready:
            return
        self.upd_btn.configure(state="disabled", text="Updating…")

        def work():
            try:
                if (BIN_DIR / ("yt-dlp" + EXE)).exists():
                    install_ytdlp()
                    self.ytdlp = find_tool("yt-dlp")
                else:
                    run([self.ytdlp, "-U"], capture_output=True, timeout=120)
                self.post(self.flash, "yt-dlp updated.", GREEN)
            except Exception as ex:
                self.post(self.flash, f"Update failed: {ex}", RED)
            self.post(lambda: self.upd_btn.winfo_exists() and self.upd_btn.configure(state="normal", text="Update yt-dlp"))

        threading.Thread(target=work, daemon=True).start()

    # ---- tool setup ---------------------------------------------------------- #
    def _banner(self, text, frac=None, error=False):
        self._banner_state = (text, frac, error)
        self.banner_lbl.configure(text=text, text_color=RED if error else TEXT)
        if frac is not None:
            self.banner_bar.set(frac)

    def _cb(self, label):
        last = [-1]

        def cb(pct):
            if int(pct) != last[0]:
                last[0] = int(pct)
                self.post(self._banner, f"{label}  {int(pct)}%", pct / 100)
        return cb

    def _setup(self):
        try:
            self.ytdlp = find_tool("yt-dlp")
            if not self.ytdlp:
                self.post(self._banner, "Installing yt-dlp…", 0)
                install_ytdlp(self._cb("Installing yt-dlp"))
                self.ytdlp = find_tool("yt-dlp")
            self.ffmpeg = find_tool("ffmpeg")
            if not (self.ffmpeg and find_tool("ffprobe")):
                self.post(self._banner, "Installing ffmpeg (one-time, ~30–80 MB)…", 0)
                install_ffmpeg(self._cb("Installing ffmpeg"))
                self.ffmpeg = find_tool("ffmpeg")
            if not find_tool("deno"):
                try:
                    self.post(self._banner, "Installing JavaScript runtime for YouTube…", 0)
                    install_deno(self._cb("Installing JS runtime"))
                except Exception:
                    pass  # optional
            if not find_tool("aria2c"):
                try:
                    self.post(self._banner, "Installing multi-segment downloader…", 0)
                    install_aria2(self._cb("Installing multi-segment downloader"))
                except Exception:
                    pass  # optional - single-connection downloads still work
            self.post(self._setup_done)
        except Exception as ex:
            self.post(self._setup_failed, str(ex))

    def _setup_done(self):
        self.ready = True
        self.banner.pack_forget()
        self.btn_paste.configure(state="normal")
        self.btn_download.configure(state="normal")

    def _setup_failed(self, msg):
        self._banner(f"Setup failed: {msg}", 0, error=True)
        self.banner_retry.pack(anchor="w", padx=16, pady=(0, 12))

    def _retry_setup(self):
        self.banner_retry.pack_forget()
        self._banner("Preparing…", 0)
        threading.Thread(target=self._setup, daemon=True).start()

    # ---- adding links -------------------------------------------------------- #
    def paste_link(self):
        try:
            txt = self.clipboard_get()
        except tk.TclError:
            txt = ""
        self._add_links(txt, "Your clipboard doesn't contain a link.")

    def add_from_entry(self):
        txt = self.entry.get()
        if self._add_links(txt, "Please enter a valid http(s) link."):
            self.entry.delete(0, "end")

    def start_all(self):
        txt = self.entry.get()
        if txt.strip():
            if self._add_links(txt, "Please enter a valid http(s) link."):
                self.entry.delete(0, "end")
        ready = [j for j in sorted(self.jobs, key=lambda j: j.seq) if j.state == "ready"]
        if not ready:
            self.flash("Nothing to download yet — paste a link first.", RED)
            return
        for j in ready:
            j.state = "queued"
            j.row.refresh()
        self.flash(f"Started {len(ready)} download{'s' if len(ready) != 1 else ''}…")
        self._pump()
        self._update_dl_btn()

    def start_job(self, job):
        if job.state == "ready":
            job.state = "queued"
            job.row.refresh()
            self._pump()
            self._update_dl_btn()

    def _add_links(self, text, err):
        urls = re.findall(r"https?://[^\s]+", text or "")
        if not urls:
            self.flash(err, RED)
            return False
        if not self.ready:
            self.flash("Still setting up yt-dlp and ffmpeg — one moment…")
            return False
        jobs = [self._new_job(u) for u in urls[:30]]
        for j in reversed(jobs):      # keep on-screen order = order of the links
            self._add_job(j)
        self._relayout()
        return True

    def _new_job(self, url, parent=None):
        url = to_playlist_url(url)
        m = self.s["mode"]
        if parent:
            j = Job(url, parent.mode, parent.cap, parent.fmt, parent.bitrate, parent.outdir)
        else:
            cap = QUALITY_V[self.s["q_video"]] if m == "video" else 0
            br = QUALITY_A[self.s["q_audio"]] if m == "audio" else None
            j = Job(url, m, cap, self.s["f_" + m].lower(), br, self.s["outdir"])
        self.seq += 1
        j.seq = self.seq
        return j

    def _add_job(self, job, at_top=True):
        job.row = Row(self.listf, self, job)
        if at_top:
            self.jobs.insert(0, job)
        else:
            self.jobs.append(job)
        self.info_q.put(job)

    def _relayout(self, *_):
        for j in self.jobs:
            j.row.pack_forget()
        shown = 0
        for j in self.jobs:
            j.row.pack(fill="x", padx=4, pady=5)
            shown += 1
        if shown:
            self.empty.pack_forget()
        else:
            self.empty.pack(pady=90)
        self._update_dl_btn()

    def _update_dl_btn(self):
        n = sum(1 for j in self.jobs if j.state == "ready")
        if getattr(self, "btn_download", None):
            self.btn_download.configure(text="Download" + (f" ({n})" if n else ""))

    # ---- info fetching ------------------------------------------------------- #
    def _info_loop(self):
        while True:
            job = self.info_q.get()
            try:
                self._info_worker(job)
            except Exception as ex:
                self.post(self._job_error, job, str(ex))

    def _info_worker(self, job):
        if job.cancel:
            return
        job.state = "fetching"
        r = run([self.ytdlp, "-J", "--no-warnings", "--flat-playlist", "--no-playlist", job.url],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=180, env=tool_env())
        if job.cancel:
            return
        if r.returncode != 0:
            lines = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
            raise RuntimeError(re.sub(r"^ERROR:\s*", "", lines[-1]) if lines else "Could not read this link")
        info = json.loads(r.stdout)

        if info.get("entries") is not None and info.get("_type") in ("playlist", "multi_video"):
            self.post(self._expand_playlist, job, info)
            return

        sel = pick_formats(info, job.mode, job.cap, job.fmt)
        job.title = info.get("title") or job.url
        job.uploader = info.get("uploader") or info.get("channel") or ""
        job.duration = info.get("duration") or 0
        job.height, job.fps, job.fmt_sel = sel["height"], sel["fps"], sel["fmt"]
        job.stream_sizes = sel["sizes"]
        job.ext_label = sel["ext"]
        size = sum(sel["sizes"])
        if job.mode == "audio" and job.duration and job.bitrate and job.fmt in ("mp3", "m4a", "opus"):
            size = int(job.duration * int(job.bitrate[:-1]) * 125)
        job.size = size

        img = None
        turl = info.get("thumbnail") or (info.get("thumbnails") or [{}])[-1].get("url")
        if turl:
            try:
                job.thumb_path = str(THUMB_DIR / f"{job.id}.jpg")
                img = fetch_thumb(turl, job.thumb_path)
            except Exception:
                job.thumb_path = None
        self.post(self._info_ready, job, img)

    def _info_ready(self, job, img):
        if job.cancel or job not in self.jobs:
            return
        if img is not None:
            job.row.set_thumb(img)
        job.state = "ready"
        job.row.refresh()
        self._update_dl_btn()
        self._pump()

    def _expand_playlist(self, parent, info):
        if parent not in self.jobs:
            return
        entries = [e for e in (info.get("entries") or []) if e][:300]
        folder = os.path.join(parent.outdir, safe_name(info.get("title")))
        idx = self.jobs.index(parent)
        parent.row.destroy()
        self.jobs.remove(parent)
        kids = []
        for e in entries:
            u = e.get("webpage_url") or e.get("url")
            if u and not u.startswith("http") and e.get("id"):
                u = f"https://www.youtube.com/watch?v={e['id']}"
            if not u:
                continue
            k = self._new_job(u, parent)
            k.outdir = folder
            k.title = e.get("title") or u
            k.row = Row(self.listf, self, k)
            kids.append(k)
            self.info_q.put(k)
        self.jobs[idx:idx] = kids
        self._relayout()
        self.flash(f"Playlist “{ellipsize(info.get('title') or '', 40)}”: {len(kids)} videos added.")

    def _job_error(self, job, msg):
        if job.cancel or job not in self.jobs:
            return
        job.state, job.error = "error", msg
        job.row.refresh()
        self._pump()

    # ---- queue / download ---------------------------------------------------- #
    def _pump(self):
        active = sum(1 for j in self.jobs if j.state in ("downloading", "processing"))
        for j in sorted((j for j in self.jobs if j.state == "queued"), key=lambda j: j.seq):
            if active >= self.s["parallel"]:
                break
            j.state = "downloading"
            j.row.refresh()
            active += 1
            threading.Thread(target=self._download_worker, args=(j,), daemon=True).start()

    def _build_cmd(self, job, use_aria=True):
        os.makedirs(job.outdir, exist_ok=True)
        cmd = [self.ytdlp, "--ignore-config", "--newline", "--no-colors", "--no-playlist",
               "--ffmpeg-location", str(Path(self.ffmpeg).parent), "-N", str(FRAG_THREADS),
               "--progress-template",
               "download:[PROG] %(progress.downloaded_bytes)s %(progress.total_bytes)s "
               "%(progress.total_bytes_estimate)s %(progress.speed)s %(progress.eta)s",
               "--print", "after_move:FINAL|%(filepath)s", "--no-quiet",
               "-o", os.path.join(job.outdir, "%(title).150B.%(ext)s")]
        if use_aria and find_tool("aria2c"):
            # Split each file into SEGMENTS parallel range requests (aria2c),
            # while HLS/DASH manifests stay on the native downloader + -N fragments.
            cmd += ["--downloader", "aria2c",
                    "--downloader", "dash,m3u8:native",
                    "--downloader-args",
                    f"aria2c:-x {SEGMENTS} -s {SEGMENTS} -k 1M --file-allocation=none --auto-save-interval=1"]
        if job.mode == "video":
            cmd += ["-f", job.fmt_sel or "bv*+ba/b", "--merge-output-format", job.fmt]
        else:
            cmd += ["-f", "bestaudio/best", "-x", "--audio-format", job.fmt,
                    "--audio-quality", job.bitrate or "0"]
        if self.s["embed_meta"]:
            cmd.append("--embed-metadata")
        if self.s["embed_thumb"] and job.fmt in ("mp3", "m4a", "flac", "mp4", "mkv"):
            cmd += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
        cmd.append(job.url)
        return cmd

    def _prog(self, job, frac, text):
        if job in self.jobs and job.state == "downloading":
            job.frac = frac
            job.row.set_progress(frac, text)

    def _set_state(self, job, state):
        if job in self.jobs:
            job.state = state
            job.row.refresh()

    def _download_worker(self, job, use_aria=True):
        started = time.time()
        final = guess = None
        n_dest = 0
        tail = []
        last_post = 0.0
        try:
            proc = popen(self._build_cmd(job, use_aria), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", env=tool_env(), bufsize=1)
            job.proc = proc
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                tail.append(line)
                tail = tail[-12:]
                pg = parse_progress(line)
                if pg is not None:
                    if time.time() - last_post < 0.15:
                        continue
                    last_post = time.time()
                    d, tot, sp, eta = pg
                    sizes = job.stream_sizes
                    idx = max(n_dest - 1, 0)
                    if len(sizes) > 1 and sum(sizes) > 0:
                        done_b = sum(sizes[:idx]) + d
                        frac = done_b / sum(sizes)
                        remaining = max(sum(sizes) - done_b, 0)
                    else:
                        frac = (d / tot) if tot else 0
                        remaining = max(tot - d, 0)
                    frac = min(frac, 0.99)
                    eta_s = remaining / sp if sp else eta
                    bits = [f"{frac * 100:.0f}%"]
                    if sp:
                        bits.append(f"{fmt_size(sp)}/s")
                    if eta_s:
                        bits.append(f"ETA {fmt_dur(eta_s) or '00:01'}")
                    self.post(self._prog, job, frac, "   ·   ".join(bits))
                elif line.startswith("FINAL|"):
                    final = line[6:]
                elif line.startswith("[download] Destination:"):
                    n_dest += 1
                    guess = line.split("Destination:", 1)[1].strip()
                elif line.startswith("[ExtractAudio] Destination:"):
                    guess = line.split("Destination:", 1)[1].strip()
                elif "Merging formats into" in line:
                    m = re.search(r'Merging formats into "(.+)"', line)
                    if m:
                        guess = m.group(1)
                elif "has already been downloaded" in line:
                    guess = line[len("[download] "):].rsplit(" has already", 1)[0]
                if line.startswith(("[Merger]", "[ExtractAudio]", "[VideoConvertor]", "[EmbedThumbnail]",
                                    "[Metadata]", "[Fixup")):
                    self.post(self._set_state, job, "processing")
            rc = proc.wait()
        except Exception as ex:
            if job.pause:
                self.post(self._paused, job)
                return
            self.post(self._finish, job, False, f"{ex}", None)
            return
        finally:
            job.proc = None

        if job.pause:
            self.post(self._paused, job)
            return
        if job.cancel:
            self.post(self._finish, job, False, "cancelled", None)
        elif rc == 0:
            path = next((p for p in (final, guess) if p and os.path.exists(p)), None)
            if not path:  # encoding mismatch fallback: newest file written since start
                files = [f for f in Path(job.outdir).glob("*") if f.is_file() and f.stat().st_mtime >= started - 2
                         and f.suffix not in (".part", ".ytdl")]
                path = str(max(files, key=lambda f: f.stat().st_mtime)) if files else None
            self.post(self._finish, job, True, "", path)
        else:
            errs = [l for l in tail if l.startswith("ERROR")]
            msg = re.sub(r"^ERROR:\s*", "", errs[-1]) if errs else (tail[-1] if tail else "Download failed")
            if use_aria and not job.cancel and not job.pause and any("aria2c exited" in l for l in tail):
                # aria2c occasionally dies mid-transfer: drop its leftovers and
                # redo this job with the plain single-stream downloader.
                for junk in (guess + ".part", guess + ".part.aria2", guess + ".aria2") if guess else ():
                    try:
                        if os.path.isfile(junk):
                            os.remove(junk)
                    except OSError:
                        pass
                self.post(self.flash, "Multi-segment download stalled — retrying without segments…", MUTED)
                self._download_worker(job, use_aria=False)
                return
            self.post(self._finish, job, False, msg, None)

    def _finish(self, job, ok, msg, path):
        if job not in self.jobs:
            self._pump()
            return
        job.pause = False
        if ok:
            job.state, job.path, job.size_est = "done", path, False
            if path and os.path.exists(path):
                job.size = os.path.getsize(path)
                job.ext_label = os.path.splitext(path)[1].lstrip(".") or job.ext_label
            self._save_history()
        elif msg == "cancelled":
            job.state = "cancelled"
        else:
            job.state, job.error = "error", msg
        job.row.refresh()
        self._pump()
        self._update_dl_btn()

    # ---- job actions --------------------------------------------------------- #
    def cancel_job(self, job):
        job.cancel = True
        kill_tree(job.proc)
        if job.state in ("fetching", "queued", "ready", "paused"):
            job.state = "cancelled"
            job.row.refresh()

    def pause_job(self, job):
        if job.state != "downloading" or job.pause:
            return
        job.pause = True
        kill_tree(job.proc)

    def resume_job(self, job):
        if job.state != "paused":
            return
        job.pause, job.error = False, ""
        if job.fmt_sel:
            job.state = "queued"
            job.row.refresh()
            self.flash("Resuming…")
            self._pump()
        else:
            job.state = "fetching"
            job.row.refresh()
            self.info_q.put(job)

    def _paused(self, job):
        if job not in self.jobs:
            self._pump()
            return
        job.state = "paused"
        job.row.refresh()
        self._pump()

    def retry_job(self, job):
        job.cancel, job.error = False, ""
        if job.fmt_sel:
            job.state = "queued"
            job.row.refresh()
            self._pump()
        else:
            job.state = "fetching"
            job.row.refresh()
            self.info_q.put(job)

    def remove_job(self, job):
        if job not in self.jobs:
            return
        job.cancel = True
        kill_tree(job.proc)
        self.jobs.remove(job)
        job.row.destroy()
        self._relayout()
        self._save_history()

    def clear_completed(self):
        for j in [j for j in self.jobs if j.state in ("done", "cancelled", "error")]:
            self.jobs.remove(j)
            j.row.destroy()
        self._relayout()
        self._save_history()

    # ---- history ------------------------------------------------------------- #
    def _save_history(self):
        save_json(HISTORY_FILE, [j.to_dict() for j in self.jobs if j.state == "done"][:200])

    def _load_history(self):
        for d in load_json(HISTORY_FILE, []):
            try:
                j = Job.from_dict(d)
                j.row = Row(self.listf, self, j)
                if j.thumb_path and os.path.exists(j.thumb_path):
                    j.row.set_thumb(Image.open(j.thumb_path).convert("RGB"))
                self.jobs.append(j)
            except Exception:
                continue


if __name__ == "__main__":
    hide_own_console()
    set_app_id()
    App().mainloop()
