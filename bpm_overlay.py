"""
BPM Overlay - always-on-top window showing the BPM of whatever your PC is playing.

Captures the default Windows output device (WASAPI loopback), so it works with
any source: browser (YouTube/SoundCloud/Beatport/Bandcamp), Spotify, Rekordbox
previews, etc.

Controls
  Left-drag ......... move the overlay
  Mouse wheel ....... shift your target range up/down by 1 BPM
  Double-click ...... flip half/double tempo (for 87 vs 174 style ambiguity)
  Right-click ....... menu (set range, pitch reach, reconnect, quit)

Requires: pip install numpy PyAudioWPatch
"""

import json
import os
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import simpledialog

import numpy as np

try:
    import pyaudiowpatch as pyaudio
except ImportError:  # allows the estimator to be imported/tested without audio libs
    pyaudio = None

# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------

SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "BPMOverlay")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
DEFAULTS = {"min_bpm": 124.0, "max_bpm": 130.0, "pitch_pct": 6.0, "x": 60, "y": 60, "alpha": 0.92,
            "device": ""}  # "" = follow the Windows default output
FOLLOW_DEFAULT = "__follow_default__"  # menu value for "Follow Windows default"

APP_VERSION = "1.1.3"

WINDOW_SEC = 10.0      # seconds of audio analysed per estimate
UPDATE_MS = 750        # how often the estimate is refreshed
SILENCE_RMS = 1e-3     # below this the output is considered silent


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    # Repair settings saved by 1.1.0/1.1.1, where picking the default stored its label
    dev = str(s.get("device") or "")
    if dev == FOLLOW_DEFAULT or dev.startswith("Follow Windows default"):
        s["device"] = ""
    return s


def save_settings(s):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------------------
# Tempo estimation (numpy only)
# --------------------------------------------------------------------------------------

def onset_envelope(x, sr, n_fft=1024, hop=256):
    """Log-magnitude spectral flux, emphasising kick/bass and percussive transients."""
    if len(x) < n_fft * 4:
        return None, None
    n_frames = 1 + (len(x) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = x[idx] * np.hanning(n_fft)[None, :]
    mag = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)

    # Group into log-spaced bands so a few loud bins can't dominate
    edges = np.geomspace(30, min(16000, sr / 2 - 1), 25)
    bands = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (freqs >= lo) & (freqs < hi)
        if sel.any():
            bands.append(mag[:, sel].mean(axis=1))
    B = np.log1p(1000.0 * np.stack(bands, axis=1))

    flux = np.maximum(0.0, np.diff(B, axis=0))
    # Low bands (kick) count a bit more - that's what DJs lock onto
    w = np.linspace(1.6, 0.8, flux.shape[1])
    env = (flux * w).sum(axis=1)

    fps = sr / hop
    # remove slow trend (local mean over ~0.4s), keep positive part
    k = max(3, int(0.4 * fps)) | 1
    trend = np.convolve(env, np.ones(k) / k, mode="same")
    env = np.maximum(0.0, env - trend)
    return env, fps


def estimate_bpm(x, sr, prior_center=125.0, lo=60.0, hi=200.0):
    """Returns (bpm, confidence 0..1) or (None, 0)."""
    env, fps = onset_envelope(x, sr)
    if env is None or env.std() < 1e-9:
        return None, 0.0
    env = env - env.mean()
    n = len(env)
    up = 8  # sinc-upsample the autocorrelation for sub-frame lag precision
    spec = np.fft.rfft(env, 2 * n)
    ac = np.fft.irfft(np.abs(spec) ** 2, 2 * n * up)[: n * up] * up
    ac /= ac[0] + 1e-12
    lag_axis = np.arange(n * up) / up
    # unbiased-ish: compensate for fewer overlapping samples at long lags
    ac = ac * (n / np.maximum(1, n - lag_axis))

    # Search one octave centred (geometrically) on the user's target range, like the
    # BPM-range setting in DJ software. Half/double is inherently ambiguous; the
    # overlay lets you flip it with a double-click.
    lo = max(lo, prior_center / np.sqrt(2))
    hi = min(hi, prior_center * np.sqrt(2))
    bpms = np.arange(lo, hi + 1e-9, 0.05)
    lags = fps * 60.0 / bpms

    # Comb over beat multiples: the true beat period lines up at every multiple,
    # a 3/2 or 2/3 impostor only at some of them.
    max_lag = n * 0.6
    score = np.zeros_like(bpms)
    count = np.zeros_like(bpms)
    for m in range(1, 9):
        L = lags * m
        valid = L < max_lag
        score[valid] += np.interp(L[valid], lag_axis, ac)
        count[valid] += 1
    score = score / np.maximum(count, 1)
    i = int(np.argmax(score))
    best = float(bpms[i])

    pos = score[score > 0]
    conf = 0.0
    if pos.size:
        conf = float(np.clip((score[i] - np.median(pos)) / (score[i] + 1e-9), 0, 1))
    return best, conf


# --------------------------------------------------------------------------------------
# Loopback capture
# --------------------------------------------------------------------------------------

class LoopbackCapture:
    def __init__(self, seconds=WINDOW_SEC + 2):
        self.seconds = seconds
        self.lock = threading.Lock()
        self.pa = None
        self.stream = None
        self.sr = 48000
        self.decim = 1
        self.buf = np.zeros(1, dtype=np.float32)
        self.pos = 0
        self.filled = 0
        self.last_data = 0.0
        self.device_name = "-"
        self.devices = []          # loopback device names seen at last (re)start
        self.missing = None        # chosen device that wasn't found
        self.error = None
        self.started_at = 0.0

    def start(self, wanted=None):
        """(Re)open capture. wanted=None follows the Windows default output;
        otherwise it's an output device name. PortAudio only re-reads the device
        list on a fresh init, which is why every start creates a new PyAudio."""
        self.stop()
        self.error = None
        self.missing = None
        self.started_at = time.time()
        try:
            self.pa = pyaudio.PyAudio()
            self.devices = [d["name"].replace(" [Loopback]", "")
                            for d in self.pa.get_loopback_device_info_generator()]
            dev = None
            if wanted:
                for lb in self.pa.get_loopback_device_info_generator():
                    if lb["name"].replace(" [Loopback]", "") == wanted:
                        dev = lb
                        break
                if dev is None:
                    self.missing = wanted
            if dev is None:
                dev = self._find_loopback()
            ch = int(dev["maxInputChannels"])
            native_sr = int(dev["defaultSampleRate"])
            self.decim = max(1, native_sr // 44100) if native_sr > 50000 else 1
            self.sr = native_sr // self.decim
            self.channels = ch
            self.buf = np.zeros(int(self.sr * self.seconds), dtype=np.float32)
            self.pos = 0
            self.filled = 0
            self.device_name = dev["name"].replace(" [Loopback]", "")
            self.stream = self.pa.open(
                format=pyaudio.paFloat32,
                channels=ch,
                rate=native_sr,
                input=True,
                input_device_index=dev["index"],
                frames_per_buffer=1024,
                stream_callback=self._callback,
            )
            self.stream.start_stream()
        except Exception as e:
            self.error = str(e)
            self.stop()

    def _find_loopback(self):
        try:
            return self.pa.get_default_wasapi_loopback()
        except Exception:
            pass
        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        speakers = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if speakers.get("isLoopbackDevice"):
            return speakers
        for lb in self.pa.get_loopback_device_info_generator():
            if speakers["name"] in lb["name"]:
                return lb
        raise RuntimeError("No WASAPI loopback device found")

    def _callback(self, in_data, frame_count, time_info, status):
        a = np.frombuffer(in_data, dtype=np.float32)
        if self.channels > 1:
            a = a.reshape(-1, self.channels).mean(axis=1)
        if self.decim > 1:
            m = len(a) // self.decim * self.decim
            a = a[:m].reshape(-1, self.decim).mean(axis=1)
        with self.lock:
            n = len(a)
            N = len(self.buf)
            if n >= N:
                self.buf[:] = a[-N:]
                self.pos = 0
            else:
                end = self.pos + n
                if end <= N:
                    self.buf[self.pos:end] = a
                else:
                    k = N - self.pos
                    self.buf[self.pos:] = a[:k]
                    self.buf[: n - k] = a[k:]
                self.pos = end % N
            self.filled = min(N, self.filled + n)
            self.last_data = time.time()
        return (None, pyaudio.paContinue)

    def latest(self, seconds):
        with self.lock:
            n = min(int(seconds * self.sr), self.filled)
            if n <= 0:
                return np.zeros(0, dtype=np.float32)
            start = (self.pos - n) % len(self.buf)
            if start + n <= len(self.buf):
                return self.buf[start:start + n].copy()
            return np.concatenate([self.buf[start:], self.buf[: (start + n) - len(self.buf)]])

    def clear(self):
        with self.lock:
            self.filled = 0

    def is_active(self):
        try:
            return self.stream is not None and self.stream.is_active()
        except Exception:
            return False

    def stop(self):
        try:
            if self.stream is not None:
                self.stream.stop_stream()
                self.stream.close()
        except Exception:
            pass
        self.stream = None
        try:
            if self.pa is not None:
                self.pa.terminate()
        except Exception:
            pass
        self.pa = None


# --------------------------------------------------------------------------------------
# Overlay UI
# --------------------------------------------------------------------------------------

BG = "#121316"
FG = "#f2f2f2"
DIM = "#8a8d94"
GREEN = "#2ecc71"
AMBER = "#f5b041"
RED = "#e74c3c"
GREY = "#3a3d44"


class Overlay:
    def __init__(self):
        self.s = load_settings()
        self.cap = LoopbackCapture()
        self.history = deque(maxlen=6)
        self.octave = 1.0          # user flip: 0.5 / 1 / 2
        self.shown_bpm = None
        self.last_good = None
        self.silent_since = None
        self._busy = 0             # >0 while a menu/dialog of ours is open

        self.root = tk.Tk()
        self.root.title("BPM Overlay")
        try:
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            self.root.iconbitmap(os.path.join(base, "bpm_overlay.ico"))
        except Exception:
            pass
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self.s["alpha"])
        self.root.geometry(f"+{int(self.s['x'])}+{int(self.s['y'])}")
        self.root.configure(bg=GREY)

        self.border = tk.Frame(self.root, bg=GREY, padx=3, pady=3)
        self.border.pack(fill="both", expand=True)
        inner = tk.Frame(self.border, bg=BG, padx=12, pady=6)
        inner.pack(fill="both", expand=True)

        top = tk.Frame(inner, bg=BG)
        top.pack(fill="x")
        self.lbl_title = tk.Label(top, text="BPM", bg=BG, fg=DIM, font=("Segoe UI", 9, "bold"))
        self.lbl_title.pack(side="left")
        self.lbl_conf = tk.Label(top, text="", bg=BG, fg=DIM, font=("Segoe UI", 9))
        self.lbl_conf.pack(side="right")

        self.lbl_bpm = tk.Label(inner, text="--", bg=BG, fg=FG, font=("Segoe UI", 34, "bold"), width=6)
        self.lbl_bpm.pack()

        self.lbl_status = tk.Label(inner, text="", bg=BG, fg=DIM, font=("Segoe UI", 9))
        self.lbl_status.pack()
        self.lbl_range = tk.Label(inner, text="", bg=BG, fg=DIM, font=("Segoe UI", 9))
        self.lbl_range.pack()

        for w in (self.root, self.border, inner, top, self.lbl_title, self.lbl_conf,
                  self.lbl_bpm, self.lbl_status, self.lbl_range):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.bind("<Double-Button-1>", self._flip_octave)
            w.bind("<Button-3>", self._menu)
            w.bind("<MouseWheel>", self._wheel)

        self._build_menu()
        self._update_range_label()

        self._start_capture()
        self.root.after(300, self._tick)
        self.root.after(2000, self._keep_on_top)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    # ---- menu / interaction -------------------------------------------------------
    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0)
        self.src_menu = tk.Menu(m, tearoff=0, postcommand=self._fill_source_menu)
        self.src_var = tk.StringVar(value=self.s.get("device") or FOLLOW_DEFAULT)
        m.add_cascade(label="Audio source", menu=self.src_menu)
        m.add_separator()
        m.add_command(label="Set target range...", command=self._ask_range)
        self.pitch_var = tk.DoubleVar(value=self.s["pitch_pct"])
        pm = tk.Menu(m, tearoff=0)
        for p in (0, 3, 4, 6, 8, 10, 16):
            pm.add_radiobutton(label=f"+/-{p}%", value=float(p), variable=self.pitch_var,
                               command=self._set_pitch)
        m.add_cascade(label="Pitch reach (amber zone)", menu=pm)
        om = tk.Menu(m, tearoff=0)
        self.alpha_var = tk.DoubleVar(value=self.s["alpha"])
        for a in (1.0, 0.92, 0.8, 0.65):
            om.add_radiobutton(label=f"{int(a * 100)}%", value=a, variable=self.alpha_var,
                               command=self._set_alpha)
        m.add_cascade(label="Opacity", menu=om)
        m.add_separator()
        m.add_command(label="Flip half/double tempo  (double-click)", command=self._flip_octave)
        m.add_command(label="Reset reading (new track)", command=self._reset)
        m.add_command(label="Reconnect audio device", command=self._reconnect)
        m.add_separator()
        m.add_command(label=f"BPM Overlay v{APP_VERSION}", state="disabled")
        m.add_command(label="Quit", command=self.quit)
        self.menu = m

    def _menu(self, e):
        # tk_popup blocks until the menu closes; pause keep-on-top meanwhile,
        # otherwise re-raising the overlay pushes it in front of its own menu.
        self._busy += 1
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()
            self._busy -= 1

    # ---- audio source ---------------------------------------------------------------
    def _start_capture(self):
        self.cap.start(self.s.get("device") or None)

    def _fill_source_menu(self):
        sm = self.src_menu
        sm.delete(0, "end")
        # Tk menus store the *label* when a radiobutton's value is "", so the
        # default entry needs a real sentinel value.
        self.src_var.set(self.s.get("device") or FOLLOW_DEFAULT)
        follow = "Follow Windows default"
        if not self.s.get("device"):
            follow += f"  (now: {self.cap.device_name})"
        sm.add_radiobutton(label=follow, value=FOLLOW_DEFAULT, variable=self.src_var,
                           command=self._choose_source)
        sm.add_separator()
        names = list(self.cap.devices)
        chosen = self.s.get("device")
        if chosen and chosen not in names:
            names.append(chosen)  # keep a missing pick visible so it can be unselected
        for name in names:
            label = name + ("  (not connected)" if name not in self.cap.devices else "")
            sm.add_radiobutton(label=label, value=name, variable=self.src_var,
                               command=self._choose_source)
        sm.add_separator()
        sm.add_command(label="Refresh device list", command=self._reconnect)

    def _choose_source(self):
        v = self.src_var.get()
        self.s["device"] = "" if v == FOLLOW_DEFAULT else v
        save_settings(self.s)
        self._reconnect()

    def _drag_start(self, e):
        self._dx = e.x_root - self.root.winfo_x()
        self._dy = e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _drag_end(self, e):
        self.s["x"], self.s["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_settings(self.s)

    def _wheel(self, e):
        step = 1.0 if e.delta > 0 else -1.0
        self.s["min_bpm"] += step
        self.s["max_bpm"] += step
        save_settings(self.s)
        self._update_range_label()
        self._render()

    def _ask_range(self):
        cur = f"{self.s['min_bpm']:g}-{self.s['max_bpm']:g}"
        self._busy += 1
        try:
            ans = simpledialog.askstring("Target BPM range", "Range (e.g. 124-130, or 128 for a single tempo):",
                                         initialvalue=cur, parent=self.root)
        finally:
            self._busy -= 1
        if not ans:
            return
        try:
            parts = [float(p) for p in ans.replace(" ", "").replace("–", "-").split("-") if p]
            lo, hi = (parts[0], parts[0]) if len(parts) == 1 else (min(parts[:2]), max(parts[:2]))
            self.s["min_bpm"], self.s["max_bpm"] = lo, hi
            save_settings(self.s)
            self._reset()
            self._update_range_label()
        except Exception:
            pass

    def _set_pitch(self):
        self.s["pitch_pct"] = float(self.pitch_var.get())
        save_settings(self.s)
        self._update_range_label()
        self._render()

    def _set_alpha(self):
        self.s["alpha"] = float(self.alpha_var.get())
        self.root.attributes("-alpha", self.s["alpha"])
        save_settings(self.s)

    def _flip_octave(self, e=None):
        self.octave = {1.0: 2.0, 2.0: 0.5, 0.5: 1.0}[self.octave]
        self._render()

    def _reset(self):
        self.history.clear()
        self.octave = 1.0
        self.shown_bpm = None
        self.cap.clear()
        self._render()

    def _reconnect(self):
        self._start_capture()
        self._reset()

    def _update_range_label(self):
        lo, hi, p = self.s["min_bpm"], self.s["max_bpm"], self.s["pitch_pct"]
        rng = f"{lo:g}" if lo == hi else f"{lo:g}-{hi:g}"
        self.lbl_range.config(text=f"target {rng}   pitch +/-{p:g}%")

    def _keep_on_top(self):
        # Some apps/full-screen windows steal topmost; re-assert periodically,
        # but never while our own menu or a dialog is open.
        if not self._busy:
            try:
                self.root.attributes("-topmost", True)
                self.root.lift()
            except Exception:
                pass
        self.root.after(2000, self._keep_on_top)

    # ---- analysis loop -------------------------------------------------------------
    def _tick(self):
        try:
            self._analyse()
        except Exception as ex:  # never let the overlay die
            self.lbl_status.config(text=f"error: {ex}"[:40], fg=RED)
        self.root.after(UPDATE_MS, self._tick)

    def _analyse(self):
        now = time.time()
        if self.cap.error or not self.cap.is_active():
            self.shown_bpm = None
            self.lbl_status.config(text="audio not connected - retrying", fg=RED)
            self._paint(None, GREY)
            if now - self.cap.started_at > 3.0:
                self._start_capture()
            return

        x = self.cap.latest(WINDOW_SEC)
        stale = now - self.cap.last_data > 1.0
        # Loudness of the most recent ~0.5 s. Needs only 0.25 s of audio: during silence
        # the buffer is cleared every tick (750 ms), so requiring a full second here
        # meant the overlay could never notice music coming back after a pause.
        recent = x[-int(0.5 * self.cap.sr):]
        rms = float(np.sqrt(np.mean(recent ** 2))) if len(recent) >= int(0.25 * self.cap.sr) else 0.0

        if stale or rms < SILENCE_RMS:
            if self.silent_since is None:
                self.silent_since = now
            # after a pause, the next track starts fresh
            if now - self.silent_since > 1.5:
                self.history.clear()
                self.cap.clear()
                self.octave = 1.0
            # Following the default: if it stays quiet, the user may have switched
            # outputs. Re-open capture so we pick up the new default device.
            if (not self.s.get("device") and now - self.silent_since > 3.0
                    and now - self.cap.started_at > 5.0):
                self._start_capture()
            self.lbl_conf.config(text="")
            if self.cap.missing:
                self.lbl_status.config(text=f"{self._short(self.cap.missing)} not found", fg=AMBER)
            else:
                self.lbl_status.config(text=f"waiting for audio on {self._short(self.cap.device_name)}",
                                       fg=DIM)
            self._paint(self.last_good, GREY, dim=True)
            return
        self.silent_since = None

        if len(x) < self.cap.sr * 4:
            self.lbl_status.config(text="listening...", fg=DIM)
            self._paint(None, GREY)
            return

        center = (self.s["min_bpm"] + self.s["max_bpm"]) / 2.0
        bpm, conf = estimate_bpm(x, self.cap.sr, prior_center=center)
        if bpm is None:
            return

        # keep readings in the same octave as the running estimate
        if self.history:
            ref = float(np.median(self.history))
            for f in (0.5, 2.0, 2.0 / 3.0, 1.5):
                if abs(bpm * f - ref) < abs(bpm - ref) and abs(bpm * f - ref) / ref < 0.03:
                    bpm *= f
            # a big jump means the track changed - start over
            if abs(bpm - ref) / ref > 0.06 and len(self.history) >= 3:
                recent = list(self.history)[-2:]
                if all(abs(bpm - r) / r > 0.06 for r in recent):
                    self.history.clear()
        self.history.append(bpm)
        self.shown_bpm = float(np.median(self.history))
        self.conf = conf
        self._render()

    # ---- drawing -------------------------------------------------------------------
    @staticmethod
    def _short(name, n=22):
        return name if len(name) <= n else name[: n - 1] + "…"

    def _classify(self, bpm):
        lo, hi, p = self.s["min_bpm"], self.s["max_bpm"], self.s["pitch_pct"] / 100.0
        if lo <= bpm <= hi:
            return GREEN, "in range"
        # can a pitch fader of +/-p% bring it into range?
        if bpm * (1 - p) <= hi and bpm * (1 + p) >= lo:
            need = (lo / bpm - 1) if bpm < lo else (hi / bpm - 1)
            return AMBER, f"pitch {need * 100:+.1f}% to fit"
        for f, tag in ((2.0, "2x"), (0.5, "1/2x")):
            if lo <= bpm * f <= hi:
                return RED, f"out - but {tag} = {bpm * f:.1f} fits"
        return RED, "out of range"

    def _render(self):
        if self.shown_bpm is None:
            self._paint(None, GREY)
            return
        bpm = self.shown_bpm * self.octave
        color, msg = self._classify(bpm)
        stable = len(self.history) >= 3 and (max(self.history) - min(self.history)) < 1.5
        if self.octave != 1.0:
            msg += f"  ({'x2' if self.octave == 2 else '/2'})"
        self.lbl_status.config(text=msg, fg=color)
        c = getattr(self, "conf", 0.0)
        self.lbl_conf.config(text=("locked" if stable and c > 0.35 else "settling..."),
                             fg=(DIM if stable else AMBER))
        self.last_good = bpm
        self._paint(bpm, color, dim=not stable)

    def _paint(self, bpm, color, dim=False):
        self.border.config(bg=color)
        self.root.configure(bg=color)
        if bpm is None:
            self.lbl_bpm.config(text="--", fg=DIM)
        else:
            self.lbl_bpm.config(text=f"{bpm:.1f}", fg=(DIM if dim else FG))

    def quit(self):
        save_settings(self.s)
        self.cap.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------------------

def _selftest():
    """Synthetic check of the estimator: python bpm_overlay.py --selftest"""
    sr = 44100
    rng = np.random.default_rng(0)
    for true in (100.0, 124.0, 128.0, 140.0, 174.0):
        t = np.arange(int(sr * WINDOW_SEC)) / sr
        x = 0.02 * rng.standard_normal(len(t))
        beat = 60.0 / true
        for i in range(int(WINDOW_SEC / beat) + 1):
            s = int(i * beat * sr)
            n = min(int(0.12 * sr), len(t) - s)
            if n <= 0:
                continue
            tt = np.arange(n) / sr
            x[s:s + n] += np.sin(2 * np.pi * 55 * tt * (1 + 2 * np.exp(-tt * 30))) * np.exp(-tt * 25)
            # offbeat hat
            h = int((i + 0.5) * beat * sr)
            if h + 2000 < len(x):
                x[h:h + 2000] += 0.3 * rng.standard_normal(2000) * np.exp(-np.arange(2000) / 300)
        bpm, conf = estimate_bpm(x.astype(np.float32), sr, prior_center=127.0)
        print(f"true {true:6.1f}  ->  est {bpm:6.2f}  conf {conf:.2f}")


def _device_test(seconds=3):
    """python bpm_overlay.py --devices : capture briefly from every output device"""
    cap = LoopbackCapture()
    cap.start()
    print("default:", cap.device_name)
    names = list(cap.devices)
    for name in names + ["Does Not Exist"]:
        cap.start(name)
        time.sleep(seconds)
        x = cap.latest(seconds)
        rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
        print(f"  {name!r:45} -> opened {cap.device_name!r:30} missing={cap.missing!r} "
              f"err={cap.error!r} audio {len(x) / cap.sr:.1f}s rms {rms:.4f}")
    cap.stop()


def _capture_test(seconds=8):
    cap = LoopbackCapture()
    cap.start()
    if cap.error:
        print("capture error:", cap.error)
        return
    print("device:", cap.device_name, "sr:", cap.sr)
    time.sleep(seconds)
    x = cap.latest(WINDOW_SEC)
    rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
    print(f"captured {len(x) / cap.sr:.1f}s, rms {rms:.4f}")
    if len(x) > cap.sr * 4 and rms > SILENCE_RMS:
        print("estimate:", estimate_bpm(x, cap.sr))
    cap.stop()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    elif "--capture-test" in sys.argv:
        _capture_test()
    elif "--devices" in sys.argv:
        _device_test()
    else:
        # Single instance: a second launch just exits
        try:
            import ctypes
            _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "BPMOverlay_SingleInstance")
            if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                sys.exit(0)
        except Exception:
            pass
        Overlay().run()
