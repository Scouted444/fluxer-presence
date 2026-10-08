#!/usr/bin/env python3
"""
fluxer_presence.py - Discord-style "rich presence" for Fluxer (Windows).

Fluxer has no native Rich Presence / plugin API yet, so this runs as a small
standalone background script. It watches what's running on your PC and sets your
Fluxer custom status to something like:

    🎮 Playing Bully · 1h 23m

Features
  - Zero dependencies (Python 3.9+ stdlib + ctypes, Windows only)
  - Foreground app wins; otherwise highest-priority running app from your list
  - Elapsed timer based on the real process start time
  - Optional window title (e.g. the file open in VS Code)
  - Clears to your ORIGINAL custom status when nothing matches / you go idle / you exit
  - Status carries an expiry, so it self-clears even if the script gets killed
  - Respects rate limits (429 Retry-After)

Setup
  1. python fluxer_presence.py        (creates config.json next to the script)
  2. Put your Fluxer token in config.json ("token") or set env var FLUXER_TOKEN
  3. python fluxer_presence.py        (leave it running; use pythonw.exe to hide the console)

Test without touching your account:  python fluxer_presence.py --dry-run

Endpoint used: PATCH https://api.fluxer.app/v1/users/@me/settings  {"custom_status": {...}}
"""

import atexit
import ctypes
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_URL = "https://api.fluxer.app/v1/users/@me/settings"
CONFIG_PATH = Path(__file__).with_name("config.json")
MAX_STATUS_LEN = 128

DEFAULT_CONFIG = {
    "token": "",
    "poll_seconds": 2,            # how often to scan for apps (local only, no network)
    "min_update_seconds": 30,     # never hit the API more often than this
    "status_ttl_minutes": 5,      # status auto-expires this long after the last push
    "idle_minutes": 10,           # no keyboard/mouse for this long = clear status (0 = off)
    "restore_original_status": True,
    # Lower priority number wins when several listed apps run and none is focused.
    # verb + name + timer are joined as "<verb> <name> · <timer>".
    # "title": true appends the window title of that app's focused window.
    "apps": {
        "bully.exe":            {"name": "Bully",           "verb": "Playing",   "emoji": "🎮", "priority": 1},
        "gmod.exe":             {"name": "Garry's Mod",     "verb": "Playing",   "emoji": "🎮", "priority": 1},
        "robloxplayerbeta.exe": {"name": "Roblox",          "verb": "Playing",   "emoji": "🎮", "priority": 1},
        "javaw.exe":            {"name": "Minecraft",       "verb": "Playing",   "emoji": "⛏️", "priority": 1},
        "blender.exe":          {"name": "Blender",         "verb": "Modeling in", "emoji": "🧊", "priority": 2},
        "unity.exe":            {"name": "Unity",           "verb": "Working in", "emoji": "🛠️", "priority": 2},
        "godot.exe":            {"name": "Godot",           "verb": "Working in", "emoji": "🛠️", "priority": 2},
        "code.exe":             {"name": "VS Code",         "verb": "Coding in", "emoji": "💻", "priority": 3, "title": True},
        "chrome.exe":           {"name": "Chrome",          "verb": "Browsing with", "emoji": "🌐", "priority": 9},
        "firefox.exe":          {"name": "Firefox",         "verb": "Browsing with", "emoji": "🌐", "priority": 9},
    },
}


# --------------------------------------------------------------------------- #
# Windows helpers (all ctypes, no pywin32/psutil)
# --------------------------------------------------------------------------- #
class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class Win:
    TH32CS_SNAPPROCESS = 0x2
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self):
        k = self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        u = self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.INVALID = ctypes.c_void_p(-1).value

        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = ctypes.c_void_p
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        k.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.GetTickCount.restype = wintypes.DWORD
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowTextW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
        u.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]

    def running(self, wanted):
        """Return {exe_lower: [pid, ...]} for processes whose exe name is in `wanted`."""
        found = {}
        snap = self.k.CreateToolhelp32Snapshot(self.TH32CS_SNAPPROCESS, 0)
        if snap is None or snap == self.INVALID:
            return found
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = self.k.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                name = entry.szExeFile.lower()
                if name in wanted:
                    found.setdefault(name, []).append(entry.th32ProcessID)
                ok = self.k.Process32NextW(snap, ctypes.byref(entry))
        finally:
            self.k.CloseHandle(snap)
        return found

    def _open(self, pid):
        return self.k.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)

    def start_time(self, pid):
        """Unix timestamp the process started at, or None."""
        h = self._open(pid)
        if not h:
            return None
        try:
            c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
            if not self.k.GetProcessTimes(h, *(ctypes.byref(x) for x in (c, e, kt, ut))):
                return None
            ticks = (c.dwHighDateTime << 32) | c.dwLowDateTime
            return ticks / 1e7 - 11644473600
        finally:
            self.k.CloseHandle(h)

    def foreground(self):
        """(exe_lower, window_title) of the focused window, or (None, '')."""
        hwnd = self.u.GetForegroundWindow()
        if not hwnd:
            return None, ""
        pid = wintypes.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        buf = ctypes.create_unicode_buffer(512)
        self.u.GetWindowTextW(hwnd, buf, 512)
        title = buf.value
        h = self._open(pid.value)
        if not h:
            return None, title
        try:
            path = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if not self.k.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size)):
                return None, title
            return os.path.basename(path.value).lower(), title
        finally:
            self.k.CloseHandle(h)

    def idle_seconds(self):
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not self.u.GetLastInputInfo(ctypes.byref(info)):
            return 0
        return ((self.k.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


# --------------------------------------------------------------------------- #
# Fluxer API
# --------------------------------------------------------------------------- #
class Fluxer:
    def __init__(self, token, dry_run=False):
        self.token = token
        self.dry_run = dry_run
        self.blocked_until = 0.0  # rate-limit cool-down

    def _request(self, method, body=None):
        req = urllib.request.Request(
            API_URL,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": self.token,
                "Content-Type": "application/json",
                "User-Agent": "fluxer-presence/1.0 (+standalone status script)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 5.0
                try:
                    wait = float(e.headers.get("Retry-After") or json.loads(e.read()).get("retry_after", 5))
                except Exception:
                    pass
                self.blocked_until = time.time() + wait + 1
                log(f"rate limited, backing off {wait:.0f}s")
                return None
            if e.code in (401, 403):
                log(f"auth rejected by Fluxer (HTTP {e.code}). Check your token.")
                if e.code == 401:
                    sys.exit(1)
            else:
                log(f"HTTP {e.code} from Fluxer")
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log(f"network error: {e}")
            return None

    def get_custom_status(self):
        if self.dry_run:
            return None
        data = self._request("GET")
        return (data or {}).get("custom_status")

    def set_custom_status(self, status):
        """status = dict(text, emoji_name?, expires_at?) or None to clear."""
        if self.dry_run:
            log(f"[dry-run] would set custom_status = {status}")
            return True
        if time.time() < self.blocked_until:
            return False
        return self._request("PATCH", {"custom_status": status}) is not None


# --------------------------------------------------------------------------- #
# Logic
# --------------------------------------------------------------------------- #
def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"created {CONFIG_PATH}")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    merged = {**DEFAULT_CONFIG, **cfg}
    merged["apps"] = {k.lower(): v for k, v in merged["apps"].items()}
    return merged


def fmt_elapsed(seconds):
    m = int(max(0, seconds) // 60)
    if m < 1:
        return "just started"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def clean_title(title, app_name):
    # "main.rs - myproject - Visual Studio Code" -> "main.rs" (first segment only)
    title = re.sub(r"\s+", " ", title).strip()
    return re.split(r"\s[-–—]\s", title, maxsplit=1)[0].strip()


def pick_app(win, cfg):
    """Return (exe, app_cfg, start_ts, title) for the app to show, or None."""
    apps = cfg["apps"]
    fg_exe, fg_title = win.foreground()
    running = win.running(set(apps))
    if not running:
        return None

    if fg_exe in running:
        exe = fg_exe
    else:
        exe = min(running, key=lambda e: apps[e].get("priority", 5))

    start = min((t for t in (win.start_time(p) for p in running[exe]) if t), default=time.time())
    title = fg_title if (exe == fg_exe and apps[exe].get("title")) else ""
    return exe, apps[exe], start, title


def build_status(exe, app, start, title, cfg):
    name = app.get("name", exe)
    text = f'{app.get("verb", "Using")} {name} · {fmt_elapsed(time.time() - start)}'
    t = clean_title(title, name) if title else ""
    if t:
        text = f"{app.get('verb', 'Using')} {name} — {t} · {fmt_elapsed(time.time() - start)}"
    if len(text) > MAX_STATUS_LEN:
        text = text[: MAX_STATUS_LEN - 1] + "…"
    status = {"text": text}
    if app.get("emoji"):
        status["emoji_name"] = app["emoji"]
    ttl = float(cfg["status_ttl_minutes"])
    if ttl > 0:
        exp = datetime.now(timezone.utc) + timedelta(minutes=ttl)
        status["expires_at"] = exp.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return status


def usable_original(status):
    """Return the user's original custom status if it's still valid, else None."""
    if not status:
        return None
    exp = status.get("expires_at")
    if exp:
        try:
            if datetime.fromisoformat(exp.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                return None
        except ValueError:
            pass
    keep = {k: status[k] for k in ("text", "emoji_id", "emoji_name", "expires_at") if status.get(k)}
    return keep or None


def main():
    if sys.platform != "win32":
        print("This script is Windows-only.")
        return 1
    dry_run = "--dry-run" in sys.argv
    cfg = load_config()
    token = (os.environ.get("FLUXER_TOKEN") or cfg.get("token") or "").strip()
    if not token and not dry_run:
        print(f"No token set. Put it in {CONFIG_PATH} (\"token\") or set FLUXER_TOKEN, then rerun.")
        return 1

    win = Win()
    fx = Fluxer(token, dry_run)
    original = usable_original(fx.get_custom_status()) if cfg["restore_original_status"] else None
    state = {"showing": False}

    def cleanup(*_):
        if state["showing"]:
            fx.blocked_until = 0
            fx.set_custom_status(original)
            state["showing"] = False
            log("status restored, bye")

    atexit.register(cleanup)
    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), lambda *_: sys.exit(0))

    # restore status when the console window is closed with the X button
    HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    handler = HANDLER(lambda ev: (cleanup(), False)[1])
    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)

    log("running - Ctrl+C to quit" + (" (dry run)" if dry_run else ""))
    last_key, last_push = None, 0.0
    poll = max(0.5, float(cfg["poll_seconds"]))
    min_gap = max(5.0, float(cfg["min_update_seconds"]))
    heartbeat = max(60.0, float(cfg["status_ttl_minutes"]) * 30)  # refresh before expiry

    while True:
        try:
            idle_limit = float(cfg["idle_minutes"]) * 60
            idle = idle_limit > 0 and win.idle_seconds() >= idle_limit
            pick = None if idle else pick_app(win, cfg)
            now = time.time()

            if pick is None:
                if state["showing"] and now - last_push >= 5 and fx.set_custom_status(original):
                    state.update(showing=False)
                    last_key, last_push = None, now
                    log("no activity - status restored")
            else:
                status = build_status(*pick, cfg)
                key = status["text"]
                due = key != last_key and now - last_push >= min_gap
                stale = now - last_push >= heartbeat
                if (due or stale) and fx.set_custom_status(status):
                    state.update(showing=True)
                    last_key, last_push = key, now
                    log(f"status -> {key}")
        except SystemExit:
            raise
        except Exception as e:  # never let one bad scan kill the loop
            log(f"error: {e!r}")
        time.sleep(poll)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
