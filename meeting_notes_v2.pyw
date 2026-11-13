"""Meeting Notes v2: record any meeting (Google Meet, Zoom, Tencent Meeting, Feishu...) from this PC,
show live captions, and write transcript + notes afterwards.

v2 (2026-11-10): fixes the blank white panel and redesigns it (liquid glass, clear states, pause).
- White box cause: the layered window was hidden (ShowWindow 0) before it ever got a picture; every later
  UpdateLayeredWindow then failed with error 87 and Windows showed Tk's plain background. Now the window is
  primed with a transparent picture before hiding, and any failed update re-arms WS_EX_LAYERED and retries.
- Per-monitor DPI: the panel is drawn for the DPI of the display it sits on.
- States: idle / recording (timer, two level meters, live captions) / paused / naming / writing notes / ready.

- Two tracks: microphone (= Me) and WASAPI loopback of the default speakers (= Others), 16 kHz WAV chunks
  of 5 minutes in <cache_dir>\\sessions\\<id>\\. Pause discards audio while paused.
- Live captions: faster-whisper large-v3-turbo on the GPU (CUDA float16) + Silero VAD, auto language.
- Stop: asks for a title, then runs meeting_notes_process_v1.py in a separate process.
- Control: tray icon, glass panel, global hotkey Ctrl+Alt+N (start / stop).
- Second launch: `meeting_notes_v2.cmd` shows the panel; `--start`, `--stop [title] [--no-ledger]`, `--pause`,
  `--resume`, `--quit` are passed to the running instance. `--demo <state> <png>` renders a state with
  sample data, saves it and exits (look checks).
Run with the venv's pythonw.exe (see meeting_notes_v2.cmd). Paths and options: meeting_notes_settings.json (README.md).
"""
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import uuid
import wave
import winreg

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import meeting_notes_process_v1 as proc  # noqa: E402

APP = "meeting_notes_v2"
SR = proc.SR
CHUNK_SEC = 300
HOTKEYS = [("Ctrl+Alt+N", ord("N"), 0x0003), ("Ctrl+Alt+Shift+N", ord("N"), 0x0007)]  # Ctrl+Alt+M is taken on this PC
CMD_EVENT = r"Local\MeetingNotesV2Cmd"
MUTEX = r"Local\MeetingNotesV2"
CMD_FILE = os.path.join(proc.CACHE, "command.json")
STATE_FILE = os.path.join(proc.CACHE, "panel_state.json")
CONSENT = "Let participants know this meeting is being recorded"
DEMO = "--demo" in sys.argv     # sample data, plain background, no state written (look checks, screenshots)
MEETING_APPS = re.compile(r"(腾讯会议|Tencent Meeting|VooV|Zoom|Google Meet|Meet -|Meet –|飞书|Feishu|Lark|会议|Teams)", re.I)


def out_label():
    """The output folder as 'Parent › Folder' for the idle panel."""
    parts = [x for x in os.path.normpath(proc.OUT_ROOT).split(os.sep) if x and not x.endswith(":")]
    return " › ".join(parts[-2:]) or proc.OUT_ROOT


def blog(msg):
    try:
        proc.log("[app] " + msg)
    except Exception:
        pass


# ---------- default audio endpoints (to follow headset switches) ----------
def default_ids():
    try:
        from pycaw.pycaw import AudioUtilities
        s = AudioUtilities.GetSpeakers()
        m = AudioUtilities.GetMicrophone()
        sid = getattr(s, "id", None) or s.GetId()
        mid = getattr(m, "id", None) or m.GetId()
        return sid, mid
    except Exception:
        return None, None


def foreground_title():
    u = ctypes.windll.user32
    h = u.GetForegroundWindow()
    n = u.GetWindowTextLengthW(h)
    b = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(h, b, n + 1)
    return b.value.strip()


# ---------- recorder ----------
class Track:
    def __init__(self, kind, sdir, sink):
        self.kind, self.sdir, self.sink = kind, sdir, sink
        self.q = queue.Queue()
        self.stream = None
        self.rs = None
        self.ch = 1
        self.written = 0
        self.last_data = time.time()
        self.idx = 0
        self.wf = None
        self.in_chunk = 0
        self.level = 0.0
        self.device = ""

    def open_test_feed(self, path):
        """Test only (env MN_TEST_FEED=<wav>): feed a WAV in real time in place of the computer audio,
        so an end-to-end test needs no sound on the speakers or earphones."""
        import soxr
        with wave.open(path, "rb") as w:
            rate, ch, raw = w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())
        self.ch = ch
        self.rs = soxr.ResampleStream(rate, SR, 1, dtype="float32")
        self.device = "test feed " + os.path.basename(path)
        step = rate // 20 * ch * 2
        self.feeding = True

        def run():
            t_next = time.time()
            for i in range(0, len(raw), step):
                if not self.feeding:
                    return
                self.q.put(raw[i:i + step])
                t_next += 0.05
                time.sleep(max(0.0, t_next - time.time()))
        threading.Thread(target=run, daemon=True).start()
        blog(f"{self.kind} opened: {self.device} {rate} Hz x{ch}")

    def open(self, p, pa):
        if self.kind == "loop" and os.environ.get("MN_TEST_FEED"):
            return self.open_test_feed(os.environ["MN_TEST_FEED"])
        w = p.get_host_api_info_by_type(pa.paWASAPI)
        if self.kind == "mic":
            dev = p.get_device_info_by_index(w["defaultInputDevice"])
        else:
            out = p.get_device_info_by_index(w["defaultOutputDevice"])
            dev = out if out.get("isLoopbackDevice") else None
            if dev is None:
                for d in p.get_loopback_device_info_generator():
                    if out["name"] in d["name"]:
                        dev = d
                        break
            if dev is None:
                raise RuntimeError("no loopback device for " + out["name"])
        rate = int(dev["defaultSampleRate"])
        self.ch = max(1, min(2, int(dev["maxInputChannels"])))
        import soxr
        self.rs = soxr.ResampleStream(rate, SR, 1, dtype="float32")
        self.device = dev["name"]

        def cb(data, frames, tinfo, status):
            self.q.put(data)
            return (None, pa.paContinue)
        self.stream = p.open(format=pa.paInt16, channels=self.ch, rate=rate, input=True,
                             input_device_index=dev["index"], frames_per_buffer=rate // 20, stream_callback=cb)
        self.stream.start_stream()
        blog(f"{self.kind} opened: {self.device} {rate} Hz x{self.ch}")

    def close_stream(self):
        try:
            if self.stream:
                self.stream.stop_stream()
                self.stream.close()
        except Exception:
            pass
        self.stream = None

    def push(self, y):
        if not len(y):
            return
        i16 = np.clip(y * 32767, -32768, 32767).astype(np.int16)
        pos = 0
        while pos < len(i16):
            if self.wf is None:
                self.idx += 1
                self.wf = wave.open(os.path.join(self.sdir, f"{self.kind}_{self.idx:04d}.wav"), "wb")
                self.wf.setnchannels(1); self.wf.setsampwidth(2); self.wf.setframerate(SR)
                self.in_chunk = 0
            take = min(len(i16) - pos, CHUNK_SEC * SR - self.in_chunk)
            self.wf.writeframes(i16[pos:pos + take].tobytes())
            self.wf._file.flush()
            self.in_chunk += take
            pos += take
            if self.in_chunk >= CHUNK_SEC * SR:
                self.wf.close()
                self.wf = None
        self.sink(self.kind, y, self.written)
        self.written += len(y)

    def drain(self, t0, paused=False):
        got = False
        while True:
            try:
                data = self.q.get_nowait()
            except queue.Empty:
                break
            got = True
            if paused:                         # paused: audio is dropped, nothing is written
                continue
            x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            if self.ch > 1:
                x = x.reshape(-1, self.ch).mean(axis=1)
            y = self.rs.resample_chunk(x)
            if len(y):
                self.level = max(self.level * 0.6, float(np.sqrt(np.mean(y * y))) * 6)
                self.push(y)
        now = time.time()
        if paused:
            self.level *= 0.5
            self.last_data = now
            return
        if got:
            self.last_data = now
        else:
            self.level *= 0.7
            if now - self.last_data > 0.5:     # loopback sends nothing while the PC is silent: keep the timeline
                gap = int((now - t0) * SR - self.written - 0.2 * SR)
                if gap > 0:
                    self.push(np.zeros(gap, dtype=np.float32))

    def finish(self):
        self.close_stream()
        if self.wf:
            self.wf.close()
            self.wf = None


class Recorder:
    def __init__(self, sdir, sink):
        self.sdir = sdir
        self.tracks = [Track("mic", sdir, sink), Track("loop", sdir, sink)]
        self.running = False
        self.error = None
        self.t0 = None             # wall-clock start (session.json)
        self.tl0 = None            # timeline origin: moves forward by each pause
        self.paused = False
        self.pause_started = None
        self.paused_total = 0.0
        self.silence = None
        self.closed = threading.Event()   # set after the final drain, when every WAV file is closed

    def start(self):
        self.running = True
        self.t0 = self.tl0 = time.time()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def pause(self):
        if not self.paused:
            self.paused, self.pause_started = True, time.time()

    def resume(self):
        if self.paused:
            d = time.time() - self.pause_started
            self.paused_total += d
            self.tl0 += d
            self.paused, self.pause_started = False, None

    def elapsed(self):
        end = self.pause_started if self.paused else time.time()
        return max(0.0, end - self.t0 - self.paused_total)

    def open_all(self):
        import pyaudiowpatch as pa
        self.pa_mod = pa
        self.p = pa.PyAudio()
        for t in self.tracks:
            t.open(self.p, pa)
        # play silence on the speakers so WASAPI loopback keeps delivering a steady stream
        if os.environ.get("MN_TEST_FEED"):
            return
        try:
            w = self.p.get_host_api_info_by_type(pa.paWASAPI)
            out = self.p.get_device_info_by_index(w["defaultOutputDevice"])
            rate, ch = int(out["defaultSampleRate"]), max(1, min(2, int(out["maxOutputChannels"])))
            zeros = bytes(rate // 20 * ch * 2)
            self.silence = self.p.open(format=pa.paInt16, channels=ch, rate=rate, output=True,
                                       output_device_index=out["index"], frames_per_buffer=rate // 20,
                                       stream_callback=lambda *a: (zeros, pa.paContinue))
            self.silence.start_stream()
        except Exception as e:
            blog(f"silence player not started: {e}")

    def close_all(self):
        for t in self.tracks:
            t.close_stream()
        try:
            if self.silence:
                self.silence.stop_stream(); self.silence.close()
        except Exception:
            pass
        self.silence = None
        try:
            self.p.terminate()
        except Exception:
            pass

    def loop(self):
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        try:
            self.open_all()
        except Exception as e:
            self.error = str(e)
            blog(f"open failed: {e}")
        ids, last_check = default_ids(), time.time()
        while self.running:
            for t in self.tracks:
                try:
                    t.drain(self.tl0, self.paused)
                except Exception as e:
                    blog(f"drain {t.kind}: {e}")
            if time.time() - last_check > 3:
                last_check = time.time()
                now_ids = default_ids()
                if now_ids != ids and None not in now_ids:
                    blog(f"default device changed {ids} -> {now_ids}; reopening")
                    ids = now_ids
                    try:
                        self.close_all()
                        self.open_all()
                        self.error = None
                    except Exception as e:
                        self.error = str(e)
            time.sleep(0.1)
        self.resume()
        for t in self.tracks:
            t.drain(self.tl0)
            t.finish()
        self.close_all()
        self.closed.set()

    def stop(self):
        self.running = False


# ---------- live captions ----------
class Live:
    def __init__(self, out_q, sdir):
        self.out_q, self.sdir = out_q, sdir
        self.buf = {"mic": [], "loop": []}
        self.start = {"mic": 0, "loop": 0}
        self.lock = threading.Lock()
        self.model = None
        self.running = True
        self.recent_others = []
        self.state = "loading"
        threading.Thread(target=self.run, daemon=True).start()

    def feed(self, kind, y, pos):
        with self.lock:
            if not self.buf[kind]:
                self.start[kind] = pos
            self.buf[kind].append(y)

    def run(self):
        try:
            self.model = proc.load_model(proc.LIVE_MODEL)
            self.state = "live"
        except Exception as e:
            self.state = "error"
            blog(f"live model failed: {e}")
            return
        jl = open(os.path.join(self.sdir, "live.jsonl"), "a", encoding="utf-8")
        while self.running:
            for kind in ("loop", "mic"):
                try:
                    self.step(kind, jl)
                except Exception as e:
                    blog(f"live step {kind}: {e}")
            time.sleep(0.6)
        for kind in ("loop", "mic"):
            try:
                self.step(kind, jl, final=True)
            except Exception:
                pass
        jl.close()
        self.model = None
        self.state = "stopped"

    def step(self, kind, jl, final=False):
        with self.lock:
            if not self.buf[kind]:
                return
            audio = np.concatenate(self.buf[kind])
            self.buf[kind] = [audio]
            base = self.start[kind]
        n = len(audio)
        if n < SR and not final:
            return
        spans = proc.speech_spans(audio, 400)
        cut = None
        if not spans:
            if n > 4 * SR or final:
                cut = max(0, n - SR // 2)
        else:
            last_end = spans[-1]["end"]
            if final or n - last_end >= SR // 2:
                cut = last_end
            elif n >= 18 * SR:
                cut = n
            if cut is not None:
                a = max(0, spans[0]["start"] - int(0.15 * SR))
                segs = proc.transcribe_piece(self.model, audio[a:cut], (base + a) / SR, beam=5)
                for s in segs:
                    if kind == "mic" and proc.drop_echo([s], self.recent_others) == []:
                        continue
                    if kind == "loop":
                        self.recent_others = (self.recent_others + [s])[-12:]
                    s["who"] = "Me" if kind == "mic" else "Others"
                    jl.write(json.dumps(s, ensure_ascii=False) + "\n")
                    jl.flush()
                    self.out_q.put(("caption", s))
        if cut is not None:
            with self.lock:
                rest = np.concatenate(self.buf[kind])[cut:]
                self.buf[kind] = [rest] if len(rest) else []
                self.start[kind] = base + cut

    def stop(self):
        self.running = False


# ---------- layered window plumbing ----------
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
HANDLE = ctypes.c_void_p
user32.GetDC.restype = HANDLE; user32.GetDC.argtypes = [HANDLE]
user32.ReleaseDC.argtypes = [HANDLE, HANDLE]
user32.GetAncestor.restype = HANDLE; user32.GetAncestor.argtypes = [HANDLE, ctypes.c_uint]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t; user32.GetWindowLongPtrW.argtypes = [HANDLE, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t; user32.SetWindowLongPtrW.argtypes = [HANDLE, ctypes.c_int, ctypes.c_ssize_t]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.UpdateLayeredWindow.argtypes = [HANDLE, HANDLE, ctypes.c_void_p, ctypes.c_void_p, HANDLE, ctypes.c_void_p,
                                       wt.DWORD, ctypes.c_void_p, wt.DWORD]
user32.MonitorFromPoint.restype = HANDLE; user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.GetMonitorInfoW.argtypes = [HANDLE, ctypes.c_void_p]
user32.ShowWindow.argtypes = [HANDLE, ctypes.c_int]
user32.SetWindowPos.argtypes = [HANDLE, HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
gdi32.CreateCompatibleDC.restype = HANDLE; gdi32.CreateCompatibleDC.argtypes = [HANDLE]
gdi32.CreateDIBSection.restype = HANDLE
gdi32.CreateDIBSection.argtypes = [HANDLE, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), HANDLE, wt.DWORD]
gdi32.SelectObject.restype = HANDLE; gdi32.SelectObject.argtypes = [HANDLE, HANDLE]
gdi32.DeleteObject.argtypes = [HANDLE]; gdi32.DeleteDC.argtypes = [HANDLE]
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x80, 0x08000000


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def push_layered(hwnd, img, x, y):
    """Per-pixel-alpha update; returns (ok, last_error)."""
    w, h = img.size
    a = np.asarray(img, dtype=np.uint16)
    al = a[..., 3:4]
    pm = (a[..., :3] * al // 255).astype(np.uint8)
    data = np.ascontiguousarray(np.concatenate([pm[..., 2:3], pm[..., 1:2], pm[..., 0:1],
                                                a[..., 3:4].astype(np.uint8)], axis=2)).tobytes()
    screen = user32.GetDC(None)
    mdc = gdi32.CreateCompatibleDC(screen)
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(mdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    ok, err = False, 0
    if hbmp and bits.value:
        ctypes.memmove(bits, data, len(data))
        old = gdi32.SelectObject(mdc, hbmp)
        blend = BLENDFUNCTION(0, 0, 255, 1)
        ctypes.set_last_error(0)
        ok = bool(user32.UpdateLayeredWindow(hwnd, screen, ctypes.byref(wt.POINT(x, y)), ctypes.byref(wt.SIZE(w, h)),
                                             mdc, ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), 2))
        err = ctypes.get_last_error()
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(hbmp)
    else:
        err = ctypes.get_last_error() or -1
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(None, screen)
    return ok, err


def rearm_layered(hwnd):
    """Clear and set WS_EX_LAYERED: brings a window whose layered updates fail (error 87) back to ULW mode."""
    ex = user32.GetWindowLongPtrW(hwnd, -20)
    user32.SetWindowLongPtrW(hwnd, -20, ex & ~WS_EX_LAYERED)
    user32.SetWindowLongPtrW(hwnd, -20, ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW)


def monitor_info(x, y):
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    hm = user32.MonitorFromPoint(wt.POINT(int(x), int(y)), 2)
    user32.GetMonitorInfoW(hm, ctypes.byref(mi))
    dpi = 96
    try:
        dx, dy = ctypes.c_uint(), ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(ctypes.c_void_p(hm), 0, ctypes.byref(dx), ctypes.byref(dy))
        dpi = dx.value or 96
    except Exception:
        pass
    return mi, dpi


def monitor_rect(x, y):
    r = monitor_info(x, y)[0].rcWork
    return r.left, r.top, r.right - r.left, r.bottom - r.top


# palette (visual-style-glass): slate glass, near-white text, sage / sand / rose, terracotta / dusty blue
TXT = (236, 240, 244)
SUB = (190, 200, 212)
FAINT = (146, 158, 174)
SAGE = (152, 188, 162)
SAND = (216, 192, 142)
ROSE = (214, 152, 152)
TERRA = (208, 152, 130)
DBLUE = (142, 174, 204)
SLATE = (24, 30, 44)
FONT_DIR = r"C:\Windows\Fonts"
FONTS = {"r": "segoeui.ttf", "sb": "seguisb.ttf", "b": "segoeuib.ttf", "cjk": "msyh.ttc", "cjkb": "msyhbd.ttc"}
CJK = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uff00-\uffef]")


class Wallpaper:
    def __init__(self):
        self.path, self.mtime, self.img = None, None, None

    def get(self):
        path = None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as k:
                path = winreg.QueryValueEx(k, "WallPaper")[0]
        except OSError:
            pass
        if not path or not os.path.exists(path):
            path = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Themes\TranscodedWallpaper")
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return None
        if path != self.path or mt != self.mtime:
            try:
                self.img = Image.open(path).convert("RGB")
                self.path, self.mtime = path, mt
            except Exception:
                self.img = None
        return self.img

    def sample(self, x, y, w, h):
        if DEMO or str(proc.S.get("background", "wallpaper")).lower() == "plain":
            return plain_backdrop(w, h)   # neutral blue-grey: no desktop content behind the glass
        img = self.get()
        if img is None:
            return Image.new("RGB", (w, h), SLATE)
        r = monitor_info(x + w // 2, y + h // 2)[0].rcMonitor
        mx, my, mw, mh = r.left, r.top, r.right - r.left, r.bottom - r.top
        iw, ih = img.size
        s = max(mw / iw, mh / ih)
        ox, oy = (iw * s - mw) / 2, (ih * s - mh) / 2
        x0, y0 = (x - mx + ox) / s, (y - my + oy) / s
        bw, bh = w / s, h / s
        x0 = min(max(0.0, x0), iw - bw)
        y0 = min(max(0.0, y0), ih - bh)
        return img.resize((w, h), Image.BILINEAR, box=(x0, y0, x0 + bw, y0 + bh))


def plain_backdrop(w, h):
    top, bottom = np.array((92, 102, 120), np.float32), np.array((58, 66, 82), np.float32)
    k = np.linspace(0.0, 1.0, max(1, h), dtype=np.float32)[:, None, None]
    return Image.fromarray(np.repeat(top * (1 - k) + bottom * k, max(1, w), axis=1).astype(np.uint8), "RGB")


def load_state():
    if DEMO:
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    if DEMO:
        return
    try:
        proc.write_atomic(STATE_FILE, json.dumps(st))
    except Exception:
        pass


def hms(sec):
    sec = int(sec)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


class App:
    W = 372      # card width, logical px
    M = 26       # margin for the shadow
    SS = 2       # supersampling
    PAD = 20

    def __init__(self):
        os.makedirs(proc.SESSIONS, exist_ok=True)
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.st = load_state()
        self.wall = Wallpaper()
        self.fonts = {}
        self.q = queue.Queue()
        self.mode = "idle"            # idle | recording | naming | processing | ready
        self.visible = False
        self.rec = self.live = None
        self.sdir = None
        self.captions = []
        self.titles = {}
        self.stage = ""
        self.result = None
        self.meeting_title = ""
        self.duration = 0
        self.actions = 0
        self.hotkey = None
        self.buttons = {}
        self.hover = None
        self.bg_cache = None
        self.drag = None
        self.moved = False
        self.ulw_fail = 0
        self.tray = None
        r = self.root
        pos = self.st.get("pos_v2")
        if pos:
            self.x, self.y = pos
        else:
            mx, my, mw, mh = monitor_rect(10, 10)
            self.x, self.y = None, None
            self.default_pos = (mx, my, mw, mh)
        mi, dpi = monitor_info(*(pos or (10, 10)))
        self.scale = dpi / 96.0
        if self.x is None:
            mx, my, mw, mh = self.default_pos
            self.x = mx + mw - self.px(self.W + self.M) - self.px(18)
            self.y = my + mh - self.px(360)
        r.geometry(f"10x10+{self.x}+{self.y}")
        r.deiconify()
        r.update_idletasks()
        self.hwnd = user32.GetAncestor(ctypes.c_void_p(r.winfo_id()), 2)
        ex = user32.GetWindowLongPtrW(self.hwnd, -20)
        user32.SetWindowLongPtrW(self.hwnd, -20, ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW)
        # prime the layered window with a transparent picture BEFORE hiding it: hiding a layered window that has
        # never had a picture leaves it in a state where every UpdateLayeredWindow fails (error 87) = white box
        push_layered(self.hwnd, Image.new("RGBA", (10, 10), (0, 0, 0, 0)), self.x, self.y)
        user32.ShowWindow(self.hwnd, 0)
        r.bind("<ButtonPress-1>", self.on_press)
        r.bind("<B1-Motion>", self.on_drag)
        r.bind("<ButtonRelease-1>", self.on_release)
        r.bind("<Motion>", self.on_motion)
        r.bind("<Leave>", lambda e: self.set_hover(None))
        self.menu = tk.Menu(r, tearoff=0)
        self.menu.add_command(label="Start / stop recording", command=self.toggle_record)
        self.menu.add_command(label="Pause / resume", command=self.toggle_pause)
        self.menu.add_command(label="Open meetings folder", command=self.open_folder)
        self.menu.add_command(label="Hide panel", command=self.hide)
        self.menu.add_separator()
        self.menu.add_command(label="Quit Meeting Notes", command=self.quit)
        r.bind("<Button-3>", self.on_menu)

    def start_threads(self):
        threading.Thread(target=self.hotkey_loop, daemon=True).start()
        threading.Thread(target=self.cmd_loop, daemon=True).start()
        threading.Thread(target=self.tray_loop, daemon=True).start()
        self.root.after(150, self.poll)
        self.root.after(3000, self.recover_unprocessed)

    # ---- helpers ----
    def px(self, v):
        return int(round(v * self.scale))

    def u(self, v):
        return int(round(v * self.scale * self.SS))

    def font(self, size, w="r", text=""):
        if text and CJK.search(text):
            w = "cjkb" if w in ("b", "sb") else "cjk"
        key = (size, w, self.scale)
        if key not in self.fonts:
            try:
                self.fonts[key] = ImageFont.truetype(os.path.join(FONT_DIR, FONTS[w]), self.u(size))
            except Exception:
                self.fonts[key] = ImageFont.load_default()
        return self.fonts[key]

    # ---- visibility ----
    def show(self):
        if not self.visible:
            self.visible = True
            self.bg_cache = None
            user32.ShowWindow(self.hwnd, 8)    # SW_SHOWNA
        self.render()

    def hide(self):
        self.visible = False
        user32.ShowWindow(self.hwnd, 0)

    def quit(self):
        if self.mode == "recording":
            self.stop_record(ask=False)
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        self.root.after(500, self.root.destroy)

    def open_folder(self, path=None):
        path = path or proc.OUT_ROOT
        os.makedirs(path, exist_ok=True)
        os.startfile(path)

    def open_notes(self):
        if self.result:
            p = os.path.join(self.result, "notes.md")
            os.startfile(p if os.path.exists(p) else self.result)

    # ---- mouse ----
    def hit(self, x_root, y_root):
        lx, ly = (x_root - self.x) / self.scale, (y_root - self.y) / self.scale
        for name, (x0, y0, x1, y1) in self.buttons.items():
            if x0 <= lx <= x1 and y0 <= ly <= y1:
                return name
        return None

    def set_hover(self, name):
        if name != self.hover:
            self.hover = name
            self.root.configure(cursor="hand2" if name else "")
            if self.visible:
                self.render()

    def on_motion(self, e):
        if not self.drag:
            self.set_hover(self.hit(e.x_root, e.y_root))

    def on_menu(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    def on_press(self, e):
        self.drag = (e.x_root - self.x, e.y_root - self.y)
        self.moved = False

    def on_drag(self, e):
        if self.drag and (abs(e.x_root - self.x - self.drag[0]) > 3 or abs(e.y_root - self.y - self.drag[1]) > 3 or self.moved):
            self.moved = True
            self.x, self.y = e.x_root - self.drag[0], e.y_root - self.drag[1]
            user32.SetWindowPos(self.hwnd, None, self.x, self.y, 0, 0, 0x0001 | 0x0004 | 0x0010)

    def on_release(self, e):
        if self.moved:
            self.st["pos_v2"] = [self.x, self.y]
            save_state(self.st)
            self.bg_cache = None
            self.render()
        else:
            name = self.hit(e.x_root, e.y_root)
            if name:
                self.click(name)
        self.drag = None

    def click(self, name):
        if name == "start":
            self.start_record()
        elif name == "stop":
            self.stop_record()
        elif name == "pause":
            self.toggle_pause()
        elif name == "save":
            self.finish_naming()
        elif name == "open":
            self.open_notes()
        elif name == "folder" and self.result:
            self.open_folder(self.result)
        elif name == "hide":
            self.hide()

    # ---- hotkey / commands / tray ----
    def hotkey_loop(self):
        for label, vk, mods in HOTKEYS:
            if user32.RegisterHotKey(None, 1, mods | 0x4000, vk):   # MOD_NOREPEAT
                self.hotkey = label
                break
        if not self.hotkey:
            blog("no hotkey could be registered")
            return
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:
                self.q.put(("toggle", None))

    def cmd_loop(self):
        k32 = ctypes.windll.kernel32
        k32.CreateEventW.restype = wt.HANDLE
        ev = k32.CreateEventW(None, False, False, CMD_EVENT)
        while ev:
            k32.WaitForSingleObject(wt.HANDLE(ev), 0xFFFFFFFF)
            try:
                with open(CMD_FILE, encoding="utf-8") as f:
                    cmd = json.load(f)
                os.remove(CMD_FILE)
            except Exception:
                cmd = {"cmd": "show"}
            self.q.put(("cmd", cmd))

    def tray_loop(self):
        try:
            import pystray
        except Exception:
            return

        def icon_img(color):
            s = 64
            im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            d.ellipse((4, 4, s - 4, s - 4), fill=(44, 54, 72, 235), outline=(220, 228, 236, 160), width=2)
            d.ellipse((20, 20, s - 20, s - 20), fill=color + (255,))
            return im
        self.tray_imgs = {"idle": icon_img(DBLUE), "rec": icon_img(ROSE), "busy": icon_img(SAND)}
        menu = pystray.Menu(
            pystray.MenuItem("Show panel", lambda: self.q.put(("cmd", {"cmd": "show"})), default=True),
            pystray.MenuItem("Start / stop recording", lambda: self.q.put(("toggle", None))),
            pystray.MenuItem("Pause / resume", lambda: self.q.put(("cmd", {"cmd": "pause_toggle"}))),
            pystray.MenuItem("Open meetings folder", lambda: self.q.put(("cmd", {"cmd": "folder"}))),
            pystray.MenuItem("Quit", lambda: self.q.put(("cmd", {"cmd": "quit"}))))
        self.tray = pystray.Icon("MeetingNotes", self.tray_imgs["idle"], "Meeting Notes (Ctrl+Alt+N)", menu)
        self.tray.run()

    def set_tray(self, key, tip):
        try:
            if self.tray:
                self.tray.icon = self.tray_imgs[key]
                self.tray.title = tip[:120]
        except Exception:
            pass

    # ---- recording ----
    def toggle_record(self):
        if self.mode == "recording":
            self.stop_record()
        elif self.mode in ("idle", "ready", "processing"):
            self.start_record()

    def toggle_pause(self):
        if self.mode != "recording":
            return
        if self.rec.paused:
            self.rec.resume()
            self.set_tray("rec", "Meeting Notes: recording (Ctrl+Alt+N stops)")
            blog("resumed")
        else:
            self.rec.pause()
            self.set_tray("busy", "Meeting Notes: paused")
            blog("paused")
        self.render()

    def start_record(self):
        if self.mode == "recording" or self.mode == "naming":
            return
        now = proc.now_tz()
        sdir = None
        try:
            os.makedirs(proc.SESSIONS, exist_ok=True)
            for _ in range(60):      # each session gets its own NEW folder (exclusive create; next second if taken)
                cand = os.path.join(proc.SESSIONS, now.strftime("%Y%m%d_%H%M%S"))
                try:
                    os.mkdir(cand)
                    sdir = cand
                    break
                except FileExistsError:
                    now += dt.timedelta(seconds=1)
        except OSError as e:
            blog(f"cannot create a session folder: {e}")
        if sdir is None:             # never fall back to an existing folder: do not record at all
            blog("recording not started: no new session folder could be created")
            self.set_tray("idle", "Meeting Notes: could not start (no new session folder)")
            return
        self.sdir = sdir
        fg = foreground_title()
        self.titles = {}
        self.first_title = fg
        self.live = Live(self.q, self.sdir)
        self.rec = Recorder(self.sdir, self.live.feed)
        self.rec.start()
        proc.write_atomic(os.path.join(self.sdir, "session.json"),
                          json.dumps({"t0": self.rec.t0, "fg_title": fg, "app": APP, "uid": uuid.uuid4().hex},
                                     ensure_ascii=False))
        self.captions = []
        self.mode = "recording"
        self.result = None
        self.set_tray("rec", "Meeting Notes: recording (Ctrl+Alt+N stops)")
        blog(f"recording started {self.sdir} (fg: {fg})")
        self.show()

    def default_title(self):
        if self.titles:
            t = max(self.titles, key=self.titles.get)
        else:
            t = self.first_title if MEETING_APPS.search(self.first_title or "") else ""
        t = re.sub(r"\s*[-–|]\s*(Google Chrome|Microsoft Edge|Mozilla Firefox|Zoom Workplace|Zoom)$", "", t).strip()
        if not t or len(t) > 60 or t.lower() in ("program manager", "meeting notes"):
            t = "Meeting"
        return t

    def stop_record(self, ask=True, title=None):
        if self.mode != "recording":
            return
        self.duration = self.rec.elapsed()
        self.rec.stop()
        self.live.stop()
        self.mode = "naming"
        self.set_tray("busy", "Meeting Notes: saving")
        self.pending_title = self.default_title()
        blog(f"recording stopped {self.sdir} ({self.duration:.0f}s, paused {self.rec.paused_total:.0f}s)")
        if title is not None or not ask:
            self.finish_naming(title or self.pending_title)
            return
        self.show()
        self.open_entry()
        self.naming_deadline = time.time() + 90

    def open_entry(self):
        """The title field is drawn inside the glass panel; keys go to the panel window itself."""
        self.title_text = self.pending_title
        self.title_fresh = True          # like a selected field: the first key replaces the suggestion
        self.root.bind("<Key>", self.on_key)
        try:
            self.root.focus_force()
        except Exception:
            pass
        self.render()

    def on_key(self, e):
        if self.mode != "naming":
            return
        ks, ch = e.keysym, e.char
        ctrl = bool(e.state & 0x4)
        if ks in ("Return", "KP_Enter"):
            self.finish_naming(self.title_text)
            return
        if ks == "Escape":
            self.finish_naming(self.pending_title)
            return
        if ks == "BackSpace":
            self.title_text = "" if self.title_fresh else self.title_text[:-1]
        elif ctrl and ks.lower() == "v":
            try:
                clip = self.root.clipboard_get().strip().replace("\n", " ")
            except Exception:
                clip = ""
            self.title_text = (clip if self.title_fresh else self.title_text + clip)[:80]
        elif ch and len(ch) == 1 and ord(ch) >= 32 and not ctrl:
            self.title_text = (ch if self.title_fresh else self.title_text + ch)[:80]
        else:
            return
        self.title_fresh = False
        self.naming_deadline = time.time() + 120
        self.render()

    def finish_naming(self, title=None):
        if self.mode != "naming":
            return
        if title is None:
            title = getattr(self, "title_text", "") or self.pending_title
        title = title.strip() or "Meeting"
        self.meeting_title = title
        rec = self.rec
        meta_p = os.path.join(self.sdir, "session.json")
        meta = None
        if os.path.exists(meta_p):
            for _ in range(10):          # a virus scanner or sync tool may hold the file for a moment
                try:
                    with open(meta_p, encoding="utf-8") as f:
                        meta = json.load(f)
                    break
                except Exception:
                    time.sleep(0.2)
            if meta is None:             # existing but unreadable: keep it untouched, process at the next start
                blog(f"session.json of {self.sdir} cannot be read; title not saved, processing deferred")
                self.mode = "failed"
                self.result = None
                self.stage = "Title not saved (session file busy): the recording is kept and processed at the next start"
                self.set_tray("idle", "Meeting Notes: " + self.stage)
                self.ready_until = time.time() + 180
                self.render()
                return
        else:
            meta = {"t0": rec.t0, "app": APP, "uid": uuid.uuid4().hex}
        meta["title"] = title
        meta["stopped"] = time.time()
        meta["paused_sec"] = round(self.rec.paused_total, 1)
        meta["ledger"] = getattr(self, "ledger", True)
        self.ledger = True
        proc.write_atomic(meta_p, json.dumps(meta, ensure_ascii=False))
        self.mode = "processing"
        self.stage = "Finishing recording"
        self.proc_dir = self.sdir
        self.proc_t0 = time.time()
        threading.Thread(target=self.run_processor, args=(self.sdir, title, False, rec), daemon=True).start()
        self.render()

    def run_processor(self, sdir, title, resume=False, rec=None):
        # the session's own recorder must have drained and closed every WAV file first
        if rec is not None and not self.recorder_closed(rec, 60):
            blog(f"recording of {sdir} did not finish closing in 60 s; processing deferred to the next start")
            try:   # only under the session lock; skipped when another process works on this session
                with proc.session_lock(sdir):
                    proc.set_status(sdir, stage="Recording still closing: it will be processed at the next start",
                                    done=True)
            except Exception:
                pass
            self.q.put(("processed", (sdir, None)))
            return
        py = os.path.join(sys.prefix, "Scripts", "python.exe")
        if not os.path.exists(py):
            py = sys.executable.replace("pythonw.exe", "python.exe")
        args = [py, "-X", "utf8", os.path.join(HERE, "meeting_notes_process_v1.py"), sdir]
        args += ["--resume"] if resume else ["--title", title]
        rc = subprocess.run(args, creationflags=proc.NO_WINDOW).returncode
        self.q.put(("processed", (sdir, rc)))

    @staticmethod
    def recorder_closed(rec, timeout):
        """True once the recorder has drained and closed its WAV files, or its thread has ended (nothing can write
        to the chunks any more); False when it is still running after `timeout` seconds."""
        end = time.time() + timeout
        while time.time() < end:
            if rec.closed.wait(0.5):
                return True
            th = getattr(rec, "thread", None)
            if th is not None and not th.is_alive():
                return True
        return rec.closed.is_set()

    def recover_unprocessed(self):
        """At start, one unfinished session is picked up (proc.recovery_action): WAV chunks and nothing saved yet ->
        full run; transcript saved but the run unfinished -> processor --resume (only the missing stages, finished ones
        and --keep-wav are kept); all done incl. verified audio but WAV chunks left -> guarded clean-up.
        Sessions touched in the last 2 minutes are left alone (they may still be written)."""
        try:
            for d in sorted(os.listdir(proc.SESSIONS)):
                sd = os.path.join(proc.SESSIONS, d)
                if sd == self.sdir or not os.path.isdir(sd):
                    continue
                files = os.listdir(sd)
                if not files or time.time() - max(os.path.getmtime(os.path.join(sd, f)) for f in files) < 120:
                    continue
                act = proc.recovery_action(sd)
                if act == "cleanup":
                    blog(f"recover: all stages done, deleting leftover WAV chunks in {sd}")
                    proc.cleanup_wavs(sd)        # re-checks: flag, notes done, audio verified and present
                    continue
                if act not in ("full", "resume"):
                    continue
                blog(f"recovering session {sd} ({act})")
                title = "Recovered meeting"
                try:
                    title = json.load(open(os.path.join(sd, "session.json"), encoding="utf-8")).get("title") or title
                except Exception:
                    pass
                threading.Thread(target=self.run_processor, args=(sd, title, act == "resume"), daemon=True).start()
                break
        except Exception as e:
            blog(f"recover: {e}")

    # ---- main loop ----
    def poll(self):
        dirty = False
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "caption":
                    self.captions = (self.captions + [val])[-30:]
                    dirty = True
                elif kind == "toggle":
                    self.toggle_record()
                elif kind == "cmd":
                    c = val.get("cmd")
                    if c == "start":
                        self.start_record()
                    elif c == "stop":
                        self.ledger = val.get("ledger", True)
                        self.stop_record(title=val.get("title"))
                    elif c in ("pause", "resume", "pause_toggle"):
                        if self.mode == "recording" and (c == "pause_toggle" or (c == "pause") != self.rec.paused):
                            self.toggle_pause()
                    elif c == "folder":
                        self.open_folder()
                    elif c == "hide":
                        self.hide()
                    elif c == "quit":
                        self.quit()
                    else:
                        self.show()
                elif kind == "processed":
                    sd_, rc_ = val if isinstance(val, tuple) else (val, None)
                    if sd_ == getattr(self, "proc_dir", None):
                        st = self.read_status(sd_)
                        stages = st.get("stages") or {}
                        notes_ok = stages.get("notes") == "done"
                        self.result = st.get("folder") if notes_ok else None
                        self.mode = "ready" if notes_ok and not st.get("error") else "failed"
                        self.audio_missing = notes_ok and stages.get("audio") is not True
                        self.stage = st.get("stage", "")
                        self.actions = st.get("actions", 0)
                        self.set_tray("idle", "Meeting Notes: " + self.stage)
                        self.ready_until = time.time() + 180
                        dirty = True
        except queue.Empty:
            pass
        if self.mode == "recording":
            ft = foreground_title()
            if ft and MEETING_APPS.search(ft):
                self.titles[ft] = self.titles.get(ft, 0) + 1
            dirty = True
        elif self.mode == "processing":
            st = self.read_status(self.proc_dir)
            if st.get("stage") and st.get("stage") != self.stage:
                self.stage = st["stage"]
                self.set_tray("busy", "Meeting Notes: " + self.stage)
            dirty = True                       # progress shimmer
        elif self.mode == "naming":
            if time.time() > getattr(self, "naming_deadline", 1e18):
                self.finish_naming()
            else:
                dirty = True
        elif self.mode in ("ready", "failed") and time.time() > getattr(self, "ready_until", 0) and not self.hover:
            self.mode = "idle"
            self.hide()
        if dirty and self.visible:
            self.render()
        self.root.after(120 if self.mode in ("recording", "processing") else 400, self.poll)

    @staticmethod
    def read_status(sdir):
        try:
            with open(os.path.join(sdir, "status.json"), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    # ---- drawing primitives ----
    def wrap(self, d, text, font, width):
        """Wrap Latin by words and Chinese by characters."""
        lines, cur = [], ""
        tokens = re.findall(r"[A-Za-z0-9\-'’.,!?;:%$()/]+\s*|\s+|.", text)
        for tk_ in tokens:
            trial = cur + tk_
            if d.textlength(trial, font=font) <= width or not cur:
                cur = trial
            else:
                lines.append(cur.rstrip())
                cur = tk_.lstrip()
        if cur.strip():
            lines.append(cur.rstrip())
        return lines

    def ellipsize(self, d, text, font, width):
        if d.textlength(text, font=font) <= width:
            return text
        while text and d.textlength(text + "…", font=font) > width:
            text = text[:-1]
        return text.rstrip() + "…"

    def round_btn(self, d, cx, cy, r, color, name, icon):
        """Glass circle button with a drawn icon."""
        U = self.u
        hov = self.hover == name
        d.ellipse((U(cx - r), U(cy - r), U(cx + r), U(cy + r)), fill=color + (88 if hov else 54,),
                  outline=color + (200 if hov else 135,), width=max(1, U(1)))
        ic = TXT + (255,)
        if icon == "rec":
            d.ellipse((U(cx - 5), U(cy - 5), U(cx + 5), U(cy + 5)), fill=ROSE + (255,))
        elif icon == "stop":
            d.rounded_rectangle((U(cx - 4.6), U(cy - 4.6), U(cx + 4.6), U(cy + 4.6)), radius=U(1.8), fill=ic)
        elif icon == "pause":
            for ox in (-3.2, 3.2):
                d.rounded_rectangle((U(cx + ox - 1.5), U(cy - 5), U(cx + ox + 1.5), U(cy + 5)), radius=U(1.2), fill=ic)
        elif icon == "play":
            d.polygon([(U(cx - 3.4), U(cy - 5.6)), (U(cx + 5.6), U(cy)), (U(cx - 3.4), U(cy + 5.6))], fill=ic)
        self.buttons[name] = (cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2)

    def close_btn(self, d, cx, cy):
        U = self.u
        hov = self.hover == "hide"
        if hov:
            d.ellipse((U(cx - 11), U(cy - 11), U(cx + 11), U(cy + 11)), fill=(255, 255, 255, 30))
        c = (TXT if hov else FAINT) + (230,)
        s = 4
        d.line((U(cx - s), U(cy - s), U(cx + s), U(cy + s)), fill=c, width=max(1, U(1.4)))
        d.line((U(cx - s), U(cy + s), U(cx + s), U(cy - s)), fill=c, width=max(1, U(1.4)))
        self.buttons["hide"] = (cx - 12, cy - 12, cx + 12, cy + 12)

    def pill_btn(self, d, x1, y0, label, color, name, h=28):
        """Pill button right-aligned at x1; returns its left edge."""
        U = self.u
        f = self.font(10.5, "sb")
        tw = d.textlength(label, font=f) / (self.scale * self.SS)
        x0 = x1 - tw - 30
        hov = self.hover == name
        d.rounded_rectangle((U(x0), U(y0), U(x1), U(y0 + h)), radius=U(h / 2),
                            fill=color + (84 if hov else 52,), outline=color + (200 if hov else 130,), width=max(1, U(1)))
        d.text((U((x0 + x1) / 2), U(y0 + h / 2)), label, font=f, fill=TXT + (255,), anchor="mm")
        self.buttons[name] = (x0, y0, x1, y0 + h)
        return x0

    def tag(self, d, x, y, label, color, size=8.5):
        """Small muted pill tag; returns its right edge."""
        U = self.u
        f = self.font(size, "sb", label)
        tw = d.textlength(label, font=f) / (self.scale * self.SS)
        h = size + 9
        d.rounded_rectangle((U(x), U(y), U(x + tw + 14), U(y + h)), radius=U(h / 2), fill=color + (46,),
                            outline=color + (110,), width=max(1, U(1)))
        d.text((U(x + 7 + tw / 2), U(y + h / 2)), label, font=f, fill=color + (255,), anchor="mm")
        return x + tw + 14

    def keycap(self, d, x, y, label):
        U = self.u
        f = self.font(9.5, "sb")
        tw = d.textlength(label, font=f) / (self.scale * self.SS)
        w = max(22, tw + 14)
        d.rounded_rectangle((U(x), U(y), U(x + w), U(y + 22)), radius=U(6), fill=(255, 255, 255, 22),
                            outline=(255, 255, 255, 64), width=max(1, U(1)))
        d.line((U(x + 5), U(y + 21), U(x + w - 5), U(y + 21)), fill=(255, 255, 255, 40), width=max(1, U(1)))
        d.text((U(x + w / 2), U(y + 11)), label, font=f, fill=TXT + (240,), anchor="mm")
        return x + w

    def meter(self, d, x, y, w, level, color, label):
        U = self.u
        f = self.font(9, "sb")
        d.text((U(x), U(y + 4)), label, font=f, fill=SUB + (255,), anchor="lm")
        lab = 26 if label == "Me" else 44
        bx = x + lab
        n = 16
        gap = 2.2
        sw = (w - lab - (n - 1) * gap) / n
        lv = min(1.0, math.sqrt(max(0.0, level)))
        lit = int(round(lv * n))
        for i in range(n):
            x0 = bx + i * (sw + gap)
            on = i < lit
            a = 225 if on else 30
            hgt = 8
            d.rounded_rectangle((U(x0), U(y + 4 - hgt / 2), U(x0 + sw), U(y + 4 + hgt / 2)), radius=U(1.6),
                                fill=(color if on else (255, 255, 255)) + (a,))

    def inset(self, d, x0, y0, x1, y1, r=13):
        U = self.u
        d.rounded_rectangle((U(x0), U(y0), U(x1), U(y1)), radius=U(r), fill=(10, 14, 24, 78),
                            outline=(255, 255, 255, 24), width=max(1, U(1)))

    def header(self, d, title, sub, dot, pulse=False, title_font=None):
        """Status orb + title + subtitle. Returns y below the header."""
        U = self.u
        pad = self.PAD
        cy = 30
        if pulse:
            p = 0.5 + 0.5 * math.sin(time.time() * 3.4)
            for k_, a_ in ((1.0, 22), (0.72, 34)):
                rr = (6 + 5 * p) * k_ + 5 * (1 - k_)
                d.ellipse((U(pad + 6 - rr), U(cy - rr), U(pad + 6 + rr), U(cy + rr)), fill=dot + (int(a_ * (1.3 - p)),))
        d.ellipse((U(pad + 1), U(cy - 5), U(pad + 11), U(cy + 5)), fill=dot + (255,))
        d.ellipse((U(pad + 3), U(cy - 3.6), U(pad + 6), U(cy - 1.4)), fill=(255, 255, 255, 90))
        ft = title_font or self.font(13, "sb", title)
        d.text((U(pad + 22), U(cy - 7)), title, font=ft, fill=TXT + (255,), anchor="lm")
        if sub:
            fs = self.font(9.5, "r", sub)
            d.text((U(pad + 22), U(cy + 10)), sub, font=fs, fill=FAINT + (255,), anchor="lm")
        return 56

    # ---- content per state ----
    def content(self):
        """Draw the panel's content; returns (RGBA image at SS scale, logical height)."""
        W, U, pad = self.W, self.u, self.PAD
        H = 420
        im = Image.new("RGBA", (U(W), U(H)), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        self.buttons = {}
        right = W - pad
        self.close_btn(d, right - 4, 30)
        bx = right - 34                         # right edge for action buttons
        mode = self.mode
        if mode == "recording":
            paused = self.rec.paused
            el = self.rec.elapsed()
            col = SAND if paused else ROSE
            app = self.default_title()
            sub = ("Nothing is recorded until you resume" if paused else
                   ("Listening to mic + computer audio" if app == "Meeting" else app))
            self.header(d, "Paused" if paused else "Recording", sub, col, pulse=not paused)
            # timer, then pause + stop
            self.round_btn(d, bx - 15, 30, 15, ROSE, "stop", "stop")
            self.round_btn(d, bx - 51, 30, 15, SAGE if paused else SAND, "pause", "play" if paused else "pause")
            ftm = self.font(15, "sb")
            d.text((U(bx - 74), U(30)), hms(el), font=ftm, fill=(TXT if not paused else SUB) + (255,), anchor="rm")
            y = 66
            L = pad + 22
            half = (W - pad - L - 18) / 2
            self.meter(d, L, y, half, 0 if paused else self.rec.tracks[0].level, TERRA, "Me")
            self.meter(d, L + half + 18, y, half, 0 if paused else self.rec.tracks[1].level, DBLUE, "Others")
            y += 20
            if self.rec.error:
                fe = self.font(9.5, "r", self.rec.error)
                d.text((U(pad), U(y + 6)), self.ellipsize(d, "Audio device problem: " + self.rec.error, fe,
                                                          U(W - 2 * pad)), font=fe, fill=ROSE + (255,), anchor="lm")
                y += 18
            # captions
            box_h = 4 * 22 + 20
            self.inset(d, pad + 8, y, W - pad + 2, y + box_h)
            fb = self.font(11.5, "r", "中")
            fw = self.font(8.5, "sb")
            lines = []
            for c in reversed(self.captions[-8:]):
                who = c.get("who", "")
                wl = self.wrap(d, c["text"], fb, U(W - pad - (pad + 70) - 8))
                if len(lines) + len(wl) > 4:
                    if not lines:              # one very long utterance: keep its last lines
                        lines = [("", ln, TERRA if who == "Me" else DBLUE, 0) for ln in wl[-4:]]
                    break
                age = len({ln[3] for ln in lines})
                lines = [(who if k == 0 else "", ln, TERRA if who == "Me" else DBLUE, age)
                         for k, ln in enumerate(wl)] + lines
            if not lines:
                msg = {"loading": "Loading live captions on the GPU…",
                       "error": "Live captions unavailable · recording continues"}.get(self.live.state, "Listening…")
                if paused:
                    msg = "Captions resume when you continue"
                d.text((U(W / 2), U(y + box_h / 2)), msg, font=self.font(10.5, "r"), fill=FAINT + (255,), anchor="mm")
            ly = y + 10 + (4 - len(lines)) * 22
            for k, (who, ln, c, age) in enumerate(lines):
                yy = ly + k * 22 + 11
                alpha = 255 if age == 0 else 175
                if who:
                    d.text((U(pad + 20), U(yy)), who, font=fw, fill=c + (alpha,), anchor="lm")
                d.text((U(pad + 70), U(yy)), ln, font=fb, fill=TXT + (alpha,), anchor="lm")
            y += box_h + 12
            fc = self.font(9, "r")
            d.ellipse((U(pad + 22), U(y + 3), U(pad + 27), U(y + 8)), fill=SAND + (220,))
            d.text((U(pad + 33), U(y + 5.5)), CONSENT, font=fc, fill=SAND + (225,), anchor="lm")
            y += 16
        elif mode == "naming":
            self.header(d, "Name this meeting", f"{hms(self.duration)} recorded · Enter saves", SAND)
            y = 66
            fx0, fx1 = pad + 8, W - pad + 2
            self.inset(d, fx0, y, fx1, y + 38, r=12)
            text = getattr(self, "title_text", "")
            fb = self.font(12, "r", text or "a")
            avail = U(fx1 - fx0 - 28)
            shown = text
            while shown and d.textlength(shown, font=fb) > avail:
                shown = shown[1:]
            tx, ty = U(fx0 + 14), U(y + 19)
            tw = d.textlength(shown, font=fb)
            if getattr(self, "title_fresh", False) and shown:
                d.rounded_rectangle((tx - U(4), ty - U(11), tx + tw + U(4), ty + U(11)), radius=U(6), fill=DBLUE + (80,))
            d.text((tx, ty), shown, font=fb, fill=TXT + (255,), anchor="lm")
            if int(time.time() * 2) % 2 == 0:
                d.line((tx + tw + U(2), ty - U(10), tx + tw + U(2), ty + U(10)), fill=TXT + (210,), width=max(1, U(1.2)))
            y += 50
            d.text((U(pad + 22), U(y + 14)), "Type a title · Esc keeps the suggestion", font=self.font(9, "r"),
                   fill=FAINT + (255,), anchor="lm")
            self.pill_btn(d, W - pad, y, "Save", SAGE, "save")
            y += 34
        elif mode == "processing":
            self.header(d, "Writing your notes", self.ellipsize(d, self.meeting_title or "Meeting",
                                                                 self.font(9.5, "r", self.meeting_title),
                                                                 U(W - 2 * pad - 70)), SAND, pulse=True)
            y = 70
            bx0, bx1 = pad + 22, W - pad
            d.rounded_rectangle((U(bx0), U(y), U(bx1), U(y + 6)), radius=U(3), fill=(255, 255, 255, 26))
            m = re.search(r"(\d+)/(\d+)", self.stage or "")
            steps = ["Finishing", "Loading audio", "Loading Whisper", "Transcribing others", "Transcribing me",
                     "Telling speakers", "Compressing", "Checking", "Writing notes"]
            k = next((i for i, s in enumerate(steps) if (self.stage or "").startswith(s)), 0)
            frac = (k + (int(m.group(1)) / max(1, int(m.group(2))) if m else 0.3)) / len(steps)
            frac = max(0.04, min(0.97, frac))
            # soft glow pulsing at the leading edge (ImageDraw overwrites alpha: glow first, bar on top)
            fx = bx0 + (bx1 - bx0) * frac
            p = 0.5 + 0.5 * math.sin(time.time() * 3.0)
            for rr, a in ((10, 22), (7, 38), (5, 60)):
                d.ellipse((U(fx - rr), U(y + 3 - rr), U(fx + rr), U(y + 3 + rr)), fill=SAND + (int(a * (0.45 + 0.55 * p)),))
            d.rounded_rectangle((U(bx0), U(y), U(fx), U(y + 6)), radius=U(3), fill=SAND + (210,))
            y += 18
            el = time.time() - getattr(self, "proc_t0", time.time())
            fs = self.font(10.5, "r")
            d.text((U(pad + 22), U(y + 8)), (self.stage or "Working") + "…", font=fs, fill=SUB + (255,), anchor="lm")
            d.text((U(W - pad), U(y + 8)), hms(el), font=self.font(9.5, "r"), fill=FAINT + (255,), anchor="rm")
            y += 24
            d.text((U(pad + 22), U(y + 6)), "You can start the next meeting any time · " + (self.hotkey or "Ctrl+Alt+N"),
                   font=self.font(9, "r"), fill=FAINT + (255,), anchor="lm")
            y += 18
        elif mode in ("ready", "failed"):
            ok = mode == "ready" and self.result
            self.header(d, "Notes ready" if ok else "Notes need a look",
                        self.ellipsize(d, self.meeting_title or "Meeting", self.font(9.5, "r", self.meeting_title),
                                       U(W - 2 * pad - 90)), SAGE if ok else ROSE)
            self.round_btn(d, bx - 15, 30, 15, SAGE, "start", "rec")
            y = 64
            x = pad + 22
            n = self.actions or 0
            x = self.tag(d, x, y, f"{n} action item{'s' if n != 1 else ''}", SAGE if n else FAINT) + 6
            if self.duration:
                x = self.tag(d, x, y, hms(self.duration), DBLUE) + 6
            if not ok:
                self.tag(d, x, y, "transcript kept", ROSE)
            elif getattr(self, "audio_missing", False):
                self.tag(d, x, y, "audio not saved · WAVs kept", ROSE)
            y += 30
            if self.result:
                parts = os.path.normpath(self.result).split(os.sep)
                link = " › ".join(parts[-2:])
                fl = self.font(9.5, "r", link)
                hov = self.hover == "folder"
                shown = self.ellipsize(d, link, fl, U(W - 2 * pad - 130))
                d.text((U(pad + 22), U(y + 14)), shown, font=fl, fill=(TXT if hov else DBLUE) + (255,), anchor="lm")
                lw = d.textlength(shown, font=fl)
                d.line((U(pad + 22), U(y + 22), U(pad + 22) + lw, U(y + 22)), fill=DBLUE + (150 if hov else 90,),
                       width=max(1, U(1)))
                self.buttons["folder"] = (pad + 18, y + 4, pad + 26 + lw / (self.scale * self.SS), y + 26)
                self.pill_btn(d, W - pad, y, "Open notes", SAGE, "open")
            else:
                d.text((U(pad + 22), U(y + 14)), self.ellipsize(d, self.stage, self.font(9.5, "r"), U(W - 2 * pad - 30)),
                       font=self.font(9.5, "r"), fill=ROSE + (255,), anchor="lm")
            y += 38
        else:
            room = U(bx - 30 - 8 - (pad + 22))         # subtitle width left of the record button
            subs = ["Captions on this PC's GPU · notes to " + out_label(), "GPU captions · notes to " + out_label(),
                    "Notes to " + out_label()]
            fs_ = self.font(9.5, "r")
            sub = next((s_ for s_ in subs if d.textlength(s_, font=self.font(9.5, "r", s_)) <= room),
                       self.ellipsize(d, subs[-1], fs_, room))
            self.header(d, "Meeting Notes", sub, DBLUE)
            self.round_btn(d, bx - 15, 30, 15, SAGE, "start", "rec")
            y = 66
            x = pad + 22
            for i, k in enumerate((self.hotkey or "Ctrl+Alt+N").split("+")):
                if i:
                    d.text((U(x + 6), U(y + 11)), "+", font=self.font(9.5, "r"), fill=FAINT + (255,), anchor="mm")
                    x += 12
                x = self.keycap(d, x, y, k)
            d.text((U(x + 10), U(y + 11)), "starts and stops a recording", font=self.font(10, "r"),
                   fill=SUB + (255,), anchor="lm")
            y += 34
        return im, y + 14

    def render(self):
        if not self.visible:
            return
        # follow the DPI of the display the panel is on
        try:
            mi, dpi = monitor_info(self.x + self.px(self.W / 2), self.y + self.px(30))
            if abs(dpi / 96.0 - self.scale) > 0.01:
                self.scale = dpi / 96.0
                self.fonts = {}
                self.bg_cache = None
        except Exception:
            pass
        try:
            under, h_log = self.content()
        except Exception as e:
            blog(f"render: {e}")
            return
        S, M, W = self.SS, self.M, self.W
        U = self.u
        cw, ch = U(W), U(h_log)
        tw, th = cw + 2 * U(M), ch + 2 * U(M)
        tw, th = tw - tw % S, th - th % S
        pw, ph = tw // S, th // S
        key = (self.x, self.y, tw, th, self.scale)
        if self.bg_cache is None or self.bg_cache[0] != key:
            mx_, my_, mw_, mh_ = monitor_rect(self.x + pw // 2, self.y + 10)
            self.x = max(mx_ - self.px(M), min(self.x, mx_ + mw_ - pw + self.px(M)))
            self.y = max(my_ - self.px(M), min(self.y, my_ + mh_ - ph + self.px(M)))
            key = (self.x, self.y, tw, th, self.scale)
            self.bg_cache = (key, self.glass(tw, th, cw, ch, pw, ph))
        out = self.bg_cache[1].copy()
        mx = U(M)
        stripe_c = {"recording": SAND if (self.rec and self.rec.paused) else ROSE, "naming": SAND,
                    "processing": SAND, "ready": SAGE, "failed": ROSE}.get(self.mode, DBLUE)
        stripe = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        ImageDraw.Draw(stripe).rounded_rectangle((mx + U(7), mx + U(20), mx + U(10), mx + ch - U(20)), radius=U(2),
                                                 fill=stripe_c + (165,))
        out.alpha_composite(stripe)
        out.alpha_composite(under.crop((0, 0, cw, ch)), (mx, mx))
        img = out.resize((pw, ph), Image.LANCZOS)
        self.last_img = img
        # the click map is in logical card coordinates; shift by the margin
        self.buttons = {k: (a + M, b + M, c + M, e + M) for k, (a, b, c, e) in self.buttons.items()}
        ok, err = push_layered(self.hwnd, img, self.x, self.y)
        if not ok:
            rearm_layered(self.hwnd)
            ok, err2 = push_layered(self.hwnd, img, self.x, self.y)
            self.ulw_fail += 1
            if self.ulw_fail <= 5 or not ok:
                blog(f"layered update failed (error {err}); re-armed: {'ok' if ok else 'still failing, error %s' % err2}")
        req = os.path.join(proc.CACHE, "snap_request")      # testing aid: save the current panel as PNG
        if os.path.exists(req):
            try:
                target = open(req, encoding="utf-8").read().strip()
                os.remove(req)
                img.save(target)
            except Exception:
                pass

    def glass(self, tw, th, cw, ch, pw, ph):
        """Frosted glass card over the wallpaper: blur, desaturate, slate tint, top sheen, hairline rim, shadow."""
        U, M = self.u, self.M
        R = U(20)
        out = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        mx = U(M)
        card = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(card).rounded_rectangle((mx, mx, mx + cw - 1, mx + ch - 1), radius=R, fill=255)
        sh = Image.new("L", (tw, th), 0)
        sh.paste(card.crop((0, 0, tw, th - U(8))), (0, U(8)))
        sh = sh.filter(ImageFilter.GaussianBlur(U(13)))
        out.alpha_composite(Image.merge("RGBA", (Image.new("L", (tw, th), 6), Image.new("L", (tw, th), 10),
                                                 Image.new("L", (tw, th), 20), sh.point(lambda v: v * 120 // 255))))
        pad = 44
        bg = self.wall.sample(self.x - pad, self.y - pad, pw + 2 * pad, ph + 2 * pad)
        bg = bg.filter(ImageFilter.GaussianBlur(28)).crop((pad, pad, pad + pw, pad + ph))
        bg = ImageEnhance.Color(bg).enhance(0.62)
        bg = Image.blend(bg, Image.new("RGB", bg.size, SLATE), 0.52)
        bg = bg.resize((tw, th), Image.BICUBIC).convert("RGBA")
        bg.putalpha(card.point(lambda v: v * 252 // 255))
        out.alpha_composite(bg)
        # top sheen (liquid glass): bright near the top edge, fading by 40 % height
        grad = Image.linear_gradient("L").resize((tw, th))
        sheen = grad.point(lambda v: max(0, 255 - v * 2) * 34 // 255)
        out.alpha_composite(Image.merge("RGBA", (Image.new("L", (tw, th), 255),) * 3 + (ImageChops.multiply(sheen, card),)))
        glow = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(glow).ellipse((mx - U(50), mx - U(80), mx + int(cw * 0.7), mx + U(40)), fill=255)
        glow = ImageChops.multiply(glow.filter(ImageFilter.GaussianBlur(U(26))), card).point(lambda v: v * 20 // 255)
        out.alpha_composite(Image.merge("RGBA", (Image.new("L", (tw, th), 255),) * 3 + (glow,)))
        # hairline rim, brighter at the top
        rim = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(rim).rounded_rectangle((mx, mx, mx + cw - 1, mx + ch - 1), radius=R, outline=255,
                                              width=max(1, U(1)))
        fade = grad.point(lambda v: 160 - v * 115 // 255)
        out.alpha_composite(Image.merge("RGBA", (Image.new("L", (tw, th), 255),) * 3 + (ImageChops.multiply(rim, fade),)))
        return out

    def run(self):
        self.root.mainloop()


# ---------- demo states (look checks without recording) ----------
class _FakeTrack:
    def __init__(self, level):
        self.level, self.wf = level, None


class _FakeRec:
    def __init__(self, paused=False):
        self.tracks = [_FakeTrack(0.32), _FakeTrack(0.55)]
        self.paused = paused
        self.error = None
        self.paused_total = 0
        self.t0 = time.time() - 754

    def elapsed(self):
        return 754


class _FakeLive:
    state = "live"


def demo(app, state):
    app.hotkey = "Ctrl+Alt+N"
    app.mode = {"paused": "recording"}.get(state, state)
    if app.mode == "recording":
        app.rec, app.live = _FakeRec(paused=state == "paused"), _FakeLive()
        app.first_title = "腾讯会议"
        app.titles = {"Weekly sync · Tencent Meeting": 3}
        app.captions = [
            {"who": "Others", "text": "Let's start with the launch plan, the December slot is confirmed."},
            {"who": "Me", "text": "Good. Can we get the final numbers by Friday?"},
            {"who": "Others", "text": "好的，我们周五之前把数字发给你。"},
        ]
    elif state == "naming":
        app.rec = _FakeRec()
        app.duration = 754
        app.pending_title = app.title_text = "Weekly sync"
        app.title_fresh = True
    elif state == "processing":
        app.meeting_title = "Weekly sync"
        app.stage = "Transcribing others 3/8"
        app.proc_t0 = time.time() - 41
    elif state == "ready":
        app.meeting_title = "Weekly sync"
        app.duration = 754
        app.actions = 4
        app.result = os.path.join(proc.OUT_ROOT, "20261110_1730_Weekly_sync")


def single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX)
    return ctypes.windll.kernel32.GetLastError() != 183


def send_command(cmd):
    os.makedirs(proc.CACHE, exist_ok=True)
    with open(CMD_FILE, "w", encoding="utf-8") as f:
        json.dump(cmd, f, ensure_ascii=False)
    k32 = ctypes.windll.kernel32
    k32.OpenEventW.restype = wt.HANDLE
    ev = k32.OpenEventW(0x0002, False, CMD_EVENT)
    if ev:
        k32.SetEvent(wt.HANDLE(ev))
        return True
    return False


def parse_cmd(argv):
    if "--start" in argv:
        return {"cmd": "start"}
    if "--stop" in argv:
        i = argv.index("--stop")
        title = argv[i + 1] if len(argv) > i + 1 and not argv[i + 1].startswith("--") else None
        return {"cmd": "stop", "title": title, "ledger": "--no-ledger" not in argv}
    for c in ("pause", "resume", "quit", "hide"):
        if "--" + c in argv:
            return {"cmd": c}
    return {"cmd": "show"}


def set_dpi_awareness():
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # per-monitor v2
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


if __name__ == "__main__":
    set_dpi_awareness()
    argv = sys.argv[1:]
    if "--demo" in argv:          # render one state with sample data, save a PNG, exit (look checks)
        i = argv.index("--demo")
        state, out_png = argv[i + 1], argv[i + 2]
        app = App()
        demo(app, state)

        def snap():
            app.show()
            app.root.after(300, lambda: (app.render(), app.last_img.save(out_png),
                                         app.root.after(int(float(os.environ.get("MN_DEMO_HOLD", "0")) * 1000),
                                                        app.root.destroy)))
        app.root.after(300, snap)
        app.run()
        sys.exit(0)
    if not single_instance():
        send_command(parse_cmd(argv))
        sys.exit(0)
    app = App()
    app.start_threads()
    if argv and "--background" not in argv:
        app.root.after(800, lambda: app.q.put(("cmd", parse_cmd(argv))))
    app.run()
