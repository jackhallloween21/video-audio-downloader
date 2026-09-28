#!/usr/bin/env python3
"""
YT Downloader
=============
A modern desktop video/audio downloader built on yt-dlp + ffmpeg.
Both tools are downloaded automatically on first run (into ~/.ytdl_gui/bin).

Run from source:   python ytdl_gui.py      (Windows: pythonw ytdl_gui.py = no console)
Build an .exe:     see README.md / .github/workflows/build.yml
"""

import io
import json
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

# Theme
BG = "#121417"
CARD = "#1c1f24"
CARD2 = "#262a31"
HOVER = "#30353d"
ACCENT = "#FFD23F"
ACCENT_H = "#F2BF1B"
TEXT = "#E8EAED"
MUTED = "#8B929C"
GREEN = "#4CD08A"
RED = "#FF6B6B"

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
}


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
        self.state = "fetching"      # fetching queued downloading processing done error cancelled
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
                          text_color="#1a1a1a" if accent else TEXT,
                          font=ctk.CTkFont(size=12, weight="bold" if accent else "normal"))
        b.pack(pady=3)

    def refresh(self):
        j, app = self.job, self.app
        self.title.configure(text=ellipsize(j.title, 90))
        self.meta.configure(text=j.meta_text())
        for w in self.actions.winfo_children():
            w.destroy()

        st = j.state
        if st == "fetching":
            self._show_bar(False)
            self.status.configure(text="Fetching info…", text_color=MUTED)
            self._btn("Cancel", lambda: app.cancel_job(j))
        elif st == "queued":
            self._show_bar(False)
            self.status.configure(text="Waiting in queue…", text_color=MUTED)
            self._btn("Cancel", lambda: app.cancel_job(j))
        elif st == "downloading":
            self._show_bar(True)
            self._btn("Cancel", lambda: app.cancel_job(j))
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
        ctk.set_appearance_mode("dark")
        self.title(APP_NAME)
        self.geometry("1000x740")
        self.minsize(860, 580)
        self.configure(fg_color=BG)

        self.s = {**DEFAULTS, **load_json(SETTINGS_FILE, {})}
        if not os.path.isdir(self.s["outdir"]):
            self.s["outdir"] = DEFAULTS["outdir"]

        self.ui_q = queue.Queue()
        self.info_q = queue.Queue()
        self.jobs = []
        self.seq = 0
        self.ytdlp = self.ffmpeg = None
        self.ready = False
        self._flash_id = None
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

    def _on_close(self):
        for j in self.jobs:
            kill_tree(j.proc)
        self.destroy()

    # ---- layout ------------------------------------------------------------ #
    def _build(self):
        F = ctk.CTkFont
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

        # toolbar card
        top = ctk.CTkFrame(self, fg_color=CARD, corner_radius=16)
        top.pack(fill="x", padx=20, pady=(4, 8))
        r1 = ctk.CTkFrame(top, fg_color="transparent")
        r1.pack(fill="x", padx=14, pady=(14, 8))
        self.btn_paste = ctk.CTkButton(r1, text="▶  Paste Link", command=self.paste_link, height=42, width=150,
                                       corner_radius=12, fg_color=ACCENT, hover_color=ACCENT_H,
                                       text_color="#1a1a1a", font=F(size=14, weight="bold"), state="disabled")
        self.btn_paste.pack(side="left")
        self.entry = ctk.CTkEntry(r1, height=42, corner_radius=12, border_width=0, fg_color=BG,
                                  placeholder_text="…or type / paste a video or playlist link and press Enter")
        self.entry.pack(side="left", fill="x", expand=True, padx=10)
        self.entry.bind("<Return>", lambda _e: self.add_from_entry())
        ctk.CTkButton(r1, text="⚙", width=42, height=42, corner_radius=12, fg_color=BG, hover_color=HOVER,
                      font=F(size=18), command=self.open_settings).pack(side="left")

        r2 = ctk.CTkFrame(top, fg_color="transparent")
        r2.pack(fill="x", padx=14, pady=(0, 14))
        opt = dict(fg_color=BG, button_color=CARD2, button_hover_color=HOVER, dropdown_fg_color=CARD,
                   dropdown_hover_color=HOVER, dropdown_text_color=TEXT, text_color=TEXT,
                   corner_radius=10, height=34)

        def lab(t):
            ctk.CTkLabel(r2, text=t, text_color=MUTED, font=F(size=12)).pack(side="left", padx=(0, 6))

        lab("Download")
        self.seg_mode = ctk.CTkSegmentedButton(r2, values=["Video", "Audio"], command=self._mode_cb,
                                               selected_color="#3B4250", selected_hover_color="#465063",
                                               unselected_color=BG, unselected_hover_color=HOVER,
                                               fg_color=BG, text_color=TEXT, height=34)
        self.seg_mode.pack(side="left", padx=(0, 18))
        lab("Quality")
        self.opt_q = ctk.CTkOptionMenu(r2, values=["Highest"], command=self._quality_cb, width=130, **opt)
        self.opt_q.pack(side="left", padx=(0, 18))
        lab("Format")
        self.opt_f = ctk.CTkOptionMenu(r2, values=["MP4"], command=self._format_cb, width=100, **opt)
        self.opt_f.pack(side="left", padx=(0, 18))
        lab("Save to")
        self.btn_dir = ctk.CTkButton(r2, text="", command=self.choose_dir, height=34, corner_radius=10,
                                     fg_color=BG, hover_color=HOVER, text_color=TEXT, anchor="w", width=180)
        self.btn_dir.pack(side="left", fill="x", expand=True)
        self._update_dir_label()

        # tabs row
        tabs = ctk.CTkFrame(self, fg_color="transparent")
        tabs.pack(fill="x", padx=22, pady=(4, 2))
        self.tabs = ctk.CTkSegmentedButton(tabs, values=["All", "Video", "Audio"], command=self._relayout,
                                           selected_color="#3B4250", selected_hover_color="#465063",
                                           unselected_color=CARD, unselected_hover_color=HOVER,
                                           fg_color=CARD, text_color=TEXT, height=30)
        self.tabs.set("All")
        self.tabs.pack(side="left")
        ctk.CTkButton(tabs, text="Clear completed", width=120, height=30, fg_color="transparent",
                      hover_color=CARD2, text_color=MUTED, command=self.clear_completed).pack(side="right")

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
                                          fg_color=ACCENT, hover_color=ACCENT_H, text_color="#1a1a1a",
                                          command=self._retry_setup)
        self.banner.pack(fill="x", padx=20, pady=(0, 8))

        self.listf = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.listf.pack(fill="both", expand=True, padx=14, pady=(2, 6))
        self.empty = ctk.CTkLabel(self.listf, text="Copy a video link, then click  Paste Link",
                                  text_color=MUTED, font=F(size=15))


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
        self.btn_dir.configure(text="  " + ellipsize(os.path.basename(self.s["outdir"].rstrip("/\\"))
                                                   or self.s["outdir"], 28))

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
        win.geometry("420x360")
        win.configure(fg_color=BG)
        win.transient(self)
        F = ctk.CTkFont
        pad = dict(padx=22, anchor="w")

        ctk.CTkLabel(win, text="Simultaneous downloads", text_color=MUTED, font=F(size=12)).pack(pady=(20, 6), **pad)
        seg = ctk.CTkSegmentedButton(win, values=["1", "2", "3", "4"], selected_color="#3B4250",
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
        self.upd_btn.pack(pady=(26, 6), **pad)
        ctk.CTkButton(win, text="Open data folder", fg_color=CARD2, hover_color=HOVER, text_color=TEXT,
                      command=lambda: open_path(str(APP_DIR))).pack(**pad)
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
            self.post(self._setup_done)
        except Exception as ex:
            self.post(self._setup_failed, str(ex))

    def _setup_done(self):
        self.ready = True
        self.banner.pack_forget()
        self.btn_paste.configure(state="normal")

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
        flt = self.tabs.get().lower()
        for j in self.jobs:
            j.row.pack_forget()
        shown = 0
        for j in self.jobs:
            if flt == "all" or flt == j.mode:
                j.row.pack(fill="x", padx=4, pady=5)
                shown += 1
        if shown:
            self.empty.pack_forget()
        else:
            self.empty.pack(pady=90)

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
        job.state = "queued"
        job.row.refresh()
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

    def _build_cmd(self, job):
        os.makedirs(job.outdir, exist_ok=True)
        cmd = [self.ytdlp, "--ignore-config", "--newline", "--no-colors", "--no-playlist",
               "--ffmpeg-location", str(Path(self.ffmpeg).parent), "-N", "4",
               "--progress-template",
               "download:[PROG] %(progress.downloaded_bytes)s %(progress.total_bytes)s "
               "%(progress.total_bytes_estimate)s %(progress.speed)s %(progress.eta)s",
               "--print", "after_move:FINAL|%(filepath)s", "--no-quiet",
               "-o", os.path.join(job.outdir, "%(title).150B.%(ext)s")]
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
            job.row.set_progress(frac, text)

    def _set_state(self, job, state):
        if job in self.jobs:
            job.state = state
            job.row.refresh()

    def _download_worker(self, job):
        started = time.time()
        final = guess = None
        n_dest = 0
        tail = []
        last_post = 0.0
        num = lambda x: float(x) if re.fullmatch(r"[\d.]+(e[+-]?\d+)?", x or "") else 0.0
        try:
            proc = popen(self._build_cmd(job), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", env=tool_env(), bufsize=1)
            job.proc = proc
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                tail.append(line)
                tail = tail[-12:]
                if line.startswith("[PROG]"):
                    parts = line.split()[1:6]
                    if len(parts) < 5 or time.time() - last_post < 0.15:
                        continue
                    last_post = time.time()
                    d, t, est, sp, eta = (num(p) for p in parts)
                    tot = t or est
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
            self.post(self._finish, job, False, f"{ex}", None)
            return
        finally:
            job.proc = None

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
            self.post(self._finish, job, False, msg, None)

    def _finish(self, job, ok, msg, path):
        if job not in self.jobs:
            self._pump()
            return
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

    # ---- job actions --------------------------------------------------------- #
    def cancel_job(self, job):
        job.cancel = True
        kill_tree(job.proc)
        if job.state in ("fetching", "queued"):
            job.state = "cancelled"
            job.row.refresh()

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
