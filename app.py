"""Freecut desktop app (Tkinter)."""
from __future__ import annotations

import bisect
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import wave
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np

from . import engine
from .engine import HOP, ANALYSIS_SR, fmt_time

try:
    import winsound
except ImportError:  # preview playback is Windows-only for now
    winsound = None

SETTINGS = Path.home() / ".freecut.json"
MEDIA_TYPES = [("Video / audio", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mts *.mxf *.mp3 *.wav *.m4a "
                                 "*.aac *.flac *.ogg *.opus"), ("All files", "*.*")]
C = dict(bg="#15161a", panel="#1f2026", raised="#2b2d35", hover="#363944", grid="#2e3038",
         text="#dfe1e8", dim="#8b909d", wave="#6cb8ff", wave_cut="#4d5566", cut="#3d2027",
         cut_off="#6b6f7c", manual="#3f2240", thr="#f2c14e", head="#ffffff", accent="#2f6fd6",
         drag="#5a3a1f")
VIEW_FLOOR_DB = -70.0
RULER = 24
PREVIEW_MAX = 300.0  # seconds of preview audio built per play

SLIDERS = [  # key, label, min, max, step, unit
    ("threshold_db", "Silence threshold", -70.0, -10.0, 0.5, "dB"),
    ("min_silence", "Minimum silence length", 0.1, 3.0, 0.05, "s"),
    ("pad_before", "Padding before speech", 0.0, 1.0, 0.01, "s"),
    ("pad_after", "Padding after speech", 0.0, 1.0, 0.01, "s"),
    ("min_sound", "Ignore sounds shorter than", 0.0, 0.5, 0.01, "s"),
]


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.info: engine.MediaInfo | None = None
        self.an: engine.Analysis | None = None
        self.silences: list[tuple[float, float]] = []
        self.silence_on: list[bool] = []
        self.kept_times: list[float] = []      # clicks that turned a silence off
        self.manual: list[tuple[float, float]] = []
        self.cuts: list[tuple[float, float]] = []
        self.keeps: list[tuple[float, float]] = []
        self.view = [0.0, 1.0]
        self.playhead = 0.0
        self.play_state = None                 # (t0, mapping, total, wav_path) while previewing
        self.busy = False
        self.cancel_flag = False
        self.msgs: queue.Queue = queue.Queue()
        self._redraw_job = None
        self._press = None
        self.settings = self._load_settings()

        root.title("Freecut")
        root.geometry("1360x800")
        root.minsize(1000, 640)
        root.configure(bg=C["bg"])
        self._style()
        self._build()
        self._bind()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(50, self._poll)

    # ------------------------------------------------------------------ UI setup
    def _style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        font = ("Segoe UI", 10)
        s.configure(".", background=C["panel"], foreground=C["text"], fieldbackground=C["bg"],
                    bordercolor=C["grid"], darkcolor=C["panel"], lightcolor=C["panel"],
                    troughcolor=C["bg"], arrowcolor=C["text"], font=font, focuscolor=C["panel"])
        s.configure("TFrame", background=C["panel"])
        s.configure("Bg.TFrame", background=C["bg"])
        s.configure("TLabel", background=C["panel"], foreground=C["text"])
        s.configure("Bar.TLabel", background=C["bg"], foreground=C["dim"])
        s.configure("Dim.TLabel", foreground=C["dim"])
        s.configure("Val.TLabel", foreground=C["thr"], font=("Segoe UI Semibold", 10))
        s.configure("Head.TLabel", foreground=C["dim"], font=("Segoe UI Semibold", 9))
        s.configure("Big.TLabel", font=("Segoe UI Semibold", 20), foreground=C["text"])
        s.configure("TButton", background=C["raised"], foreground=C["text"], padding=(10, 6),
                    borderwidth=0, relief="flat")
        s.map("TButton", background=[("disabled", C["panel"]), ("active", C["hover"])],
              foreground=[("disabled", C["dim"])])
        s.configure("Accent.TButton", background=C["accent"], foreground="white")
        s.map("Accent.TButton", background=[("disabled", "#26324a"), ("active", "#3e80ee")])
        s.configure("Small.TButton", padding=(8, 2))
        s.configure("Horizontal.TScale", background=C["raised"], troughcolor=C["bg"],
                    sliderthickness=14, gripcount=0)
        s.map("Horizontal.TScale", background=[("active", C["hover"])])
        s.configure("TCombobox", fieldbackground=C["bg"], background=C["raised"],
                    foreground=C["text"], selectbackground=C["bg"], selectforeground=C["text"],
                    padding=4)
        s.map("TCombobox", fieldbackground=[("readonly", C["bg"])])
        s.configure("Horizontal.TProgressbar", background=C["accent"], troughcolor=C["bg"],
                    borderwidth=0, thickness=6)
        s.configure("Horizontal.TScrollbar", background=C["raised"], troughcolor=C["bg"],
                    borderwidth=0, arrowsize=12)
        self.root.option_add("*TCombobox*Listbox.background", C["bg"])
        self.root.option_add("*TCombobox*Listbox.foreground", C["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", C["accent"])

    def _build(self):
        root = self.root
        top = ttk.Frame(root, style="Bg.TFrame", padding=(12, 10, 12, 6))
        top.pack(fill="x")
        self.btn_open = ttk.Button(top, text="Open video / audio…", style="Accent.TButton",
                                   command=self.open_dialog, takefocus=False)
        self.btn_open.pack(side="left")
        self.btn_play = ttk.Button(top, text="▶  Preview", command=self.toggle_play,
                                   takefocus=False, state="disabled")
        self.btn_play.pack(side="left", padx=(8, 0))
        self.file_lbl = ttk.Label(top, text="No file loaded", style="Bar.TLabel")
        self.file_lbl.pack(side="left", padx=12)
        self.time_lbl = ttk.Label(top, text="", style="Bar.TLabel")
        self.time_lbl.pack(side="right")

        body = ttk.Frame(root, style="Bg.TFrame")
        body.pack(fill="both", expand=True)

        side = ttk.Frame(body, padding=(16, 14), width=330)
        side.pack(side="right", fill="y")
        side.pack_propagate(False)

        left = ttk.Frame(body, style="Bg.TFrame", padding=(12, 0, 8, 0))
        left.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(left, bg=C["bg"], highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.hbar = ttk.Scrollbar(left, orient="horizontal", command=self._on_scrollbar)
        self.hbar.pack(fill="x", pady=(4, 0))
        ttk.Label(left, style="Bar.TLabel", text=(
            "Click red area: keep it  ·  Drag: cut by hand  ·  Right-click: undo hand cut  ·  "
            "Click: move playhead  ·  Wheel: zoom  ·  Shift+wheel: scroll  ·  Space: preview")).pack(anchor="w", pady=(4, 8))

        # --- detection controls
        ttk.Label(side, text="DETECTION", style="Head.TLabel").pack(anchor="w")
        self.vars: dict[str, tk.DoubleVar] = {}
        self.val_lbls: dict[str, ttk.Label] = {}
        defaults = engine.Params().to_dict() | self.settings.get("params", {})
        for key, label, lo, hi, step, unit in SLIDERS:
            row = ttk.Frame(side)
            row.pack(fill="x", pady=(8, 0))
            ttk.Label(row, text=label).pack(side="left")
            v = tk.DoubleVar(value=defaults[key])
            self.vars[key] = v
            lbl = ttk.Label(row, style="Val.TLabel")
            lbl.pack(side="right")
            self.val_lbls[key] = lbl
            if key == "threshold_db":
                ttk.Button(row, text="Auto", style="Small.TButton", takefocus=False,
                           command=self.auto_threshold).pack(side="right", padx=8)
            sc = ttk.Scale(side, from_=lo, to=hi, variable=v, takefocus=False,
                           command=lambda _v, k=key, st=step: self._on_slider(k, st))
            sc.pack(fill="x", pady=(4, 0))
            self._update_val_label(key)

        # --- result
        ttk.Separator(side).pack(fill="x", pady=12)
        head = ttk.Frame(side)
        head.pack(fill="x")
        ttk.Label(head, text="RESULT", style="Head.TLabel").pack(side="left")
        self.btn_reset = ttk.Button(head, text="Reset manual edits", style="Small.TButton",
                                    takefocus=False, command=self.reset_edits, state="disabled")
        self.btn_reset.pack(side="right")
        self.pct_lbl = ttk.Label(side, text="–", style="Big.TLabel")
        self.pct_lbl.pack(anchor="w", pady=(2, 0))
        self.len_lbl = ttk.Label(side, text="", style="Dim.TLabel")
        self.len_lbl.pack(anchor="w")
        self.cut_lbl = ttk.Label(side, text="", style="Dim.TLabel")
        self.cut_lbl.pack(anchor="w")

        # --- export
        ttk.Separator(side).pack(fill="x", pady=12)
        ttk.Label(side, text="EXPORT", style="Head.TLabel").pack(anchor="w")
        row = ttk.Frame(side)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Quality").pack(side="left")
        self.quality = tk.StringVar(value=self.settings.get("quality", "High"))
        ttk.Combobox(row, textvariable=self.quality, values=list(engine.VIDEO_QUALITY), width=9,
                     state="readonly", takefocus=False).pack(side="left", padx=(6, 0))
        self.speed = tk.StringVar(value=self.settings.get("speed", "fast"))
        ttk.Combobox(row, textvariable=self.speed, width=9, state="readonly", takefocus=False,
                     values=["ultrafast", "veryfast", "fast", "medium", "slow"]).pack(side="right")
        ttk.Label(row, text="Speed").pack(side="right", padx=(0, 6))
        self.export_btns = [
            ttk.Button(side, text="Export video / audio file…", style="Accent.TButton",
                       takefocus=False, command=self.export_media),
            ttk.Button(side, text="Export for Premiere / Resolve (.xml)…", takefocus=False,
                       command=lambda: self.export_timeline("xml")),
            ttk.Button(side, text="Export for Final Cut / Resolve (.fcpxml)…", takefocus=False,
                       command=lambda: self.export_timeline("fcpxml")),
        ]
        for i, b in enumerate(self.export_btns):
            b.pack(fill="x", pady=(10 if i == 0 else 6, 0))
            b.state(["disabled"])

        # --- status bar
        bar = ttk.Frame(root, style="Bg.TFrame", padding=(12, 4, 12, 8))
        bar.pack(fill="x", side="bottom", before=body)
        self.status = ttk.Label(bar, text="Open a file to get started (Ctrl+O).", style="Bar.TLabel")
        self.status.pack(side="left")
        self.btn_cancel = ttk.Button(bar, text="Cancel", style="Small.TButton", takefocus=False,
                                     command=self._cancel)
        self.progress = ttk.Progressbar(bar, length=260, maximum=1.0)

    def _bind(self):
        c = self.canvas
        c.bind("<Configure>", lambda e: self.redraw())
        c.bind("<ButtonPress-1>", self._on_press)
        c.bind("<B1-Motion>", self._on_drag)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<Button-3>", self._on_right_click)
        c.bind("<MouseWheel>", self._on_wheel)
        c.bind("<Shift-MouseWheel>", lambda e: self._on_wheel(e, pan=True))
        c.bind("<Button-4>", lambda e: self._on_wheel(e, delta=120))   # X11
        c.bind("<Button-5>", lambda e: self._on_wheel(e, delta=-120))
        self.root.bind("<Control-o>", lambda e: self.open_dialog())
        self.root.bind("<space>", self._on_space)
        self.root.bind("<Home>", lambda e: self._set_playhead(0.0))

    # ------------------------------------------------------------------ settings
    def _load_settings(self) -> dict:
        try:
            return json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _on_close(self):
        self.stop_play()
        self.cancel_flag = True
        p = self.params().to_dict()
        p.pop("threshold_db")  # threshold is set per file
        self.settings.update(params=p, quality=self.quality.get(), speed=self.speed.get())
        try:
            SETTINGS.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
        except OSError:
            pass
        self.root.destroy()

    # ------------------------------------------------------------------ background work
    def _run_bg(self, text: str, fn):
        self.busy = True
        self.cancel_flag = False
        self._set_status(text)
        self.progress["value"] = 0
        self.btn_cancel.pack(side="right", padx=(8, 0))
        self.progress.pack(side="right")
        self._update_buttons()

        def worker():
            try:
                fn()
            except engine.Cancelled:
                self.msgs.put(("status", "Cancelled."))
            except Exception as e:  # surfaced in a dialog
                self.msgs.put(("error", str(e)))
            finally:
                self.msgs.put(("idle",))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                m = self.msgs.get_nowait()
                kind = m[0]
                if kind == "progress":
                    self.progress["value"] = m[1]
                elif kind == "status":
                    self._set_status(m[1])
                elif kind == "error":
                    self._set_status("Something went wrong.")
                    messagebox.showerror("Freecut", m[1], parent=self.root)
                elif kind == "loaded":
                    self._on_loaded(m[1], m[2])
                elif kind == "exported":
                    self.root.after(100, self._on_exported, m[1])  # after the "idle" message
                elif kind == "idle":
                    self.busy = False
                    self.progress.pack_forget()
                    self.btn_cancel.pack_forget()
                    self._update_buttons()
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _cancel(self):
        self.cancel_flag = True

    def _set_status(self, text: str):
        self.status.configure(text=text)

    def _update_buttons(self):
        loaded = self.an is not None and not self.busy
        for b in self.export_btns:
            b.state(["!disabled"] if loaded else ["disabled"])
        self.btn_open.state(["disabled"] if self.busy else ["!disabled"])
        self.btn_play.state(["!disabled"] if loaded and winsound else ["disabled"])
        edits = bool(self.kept_times or self.manual)
        self.btn_reset.state(["!disabled"] if edits and loaded else ["disabled"])

    # ------------------------------------------------------------------ loading
    def open_dialog(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.root, title="Open video or audio",
                                          filetypes=MEDIA_TYPES,
                                          initialdir=self.settings.get("last_dir") or None)
        if path:
            self.open_file(path)

    def open_file(self, path: str):
        self.stop_play()
        self.settings["last_dir"] = str(Path(path).parent)
        name = Path(path).name

        def job():
            info, an = engine.load(path, lambda p: self.msgs.put(("progress", p)),
                                   lambda: self.cancel_flag)
            self.msgs.put(("loaded", info, an))
        self._run_bg(f"Analysing {name}…", job)

    def _on_loaded(self, info: engine.MediaInfo, an: engine.Analysis):
        self.info, self.an = info, an
        self.kept_times, self.manual = [], []
        self.playhead = 0.0
        self.view = [0.0, max(info.duration, 1.0)]
        self.root.title(f"Freecut — {Path(info.path).name}")
        kind = (f"{info.width}×{info.height} · {float(info.timeline_fps):.3g} fps"
                if info.has_video else "audio only")
        self.file_lbl.configure(text=f"{Path(info.path).name}   ·   {fmt_time(info.duration)}   ·   {kind}")
        self.vars["threshold_db"].set(engine.auto_threshold(an.db))
        self._update_val_label("threshold_db")
        self._set_status("Ready. Red areas will be removed.")
        self.recompute()

    # ------------------------------------------------------------------ detection
    def params(self) -> engine.Params:
        return engine.Params(**{k: float(v.get()) for k, v in self.vars.items()})

    def _update_val_label(self, key):
        unit = next(s[5] for s in SLIDERS if s[0] == key)
        v = self.vars[key].get()
        self.val_lbls[key].configure(text=f"{v:.1f} dB" if unit == "dB" else f"{v:.2f} s")

    def _on_slider(self, key, step):
        v = self.vars[key]
        snapped = round(round(v.get() / step) * step, 4)
        if abs(snapped - v.get()) > 1e-9:
            v.set(snapped)
        self._update_val_label(key)
        self.stop_play()
        if self._redraw_job:
            self.root.after_cancel(self._redraw_job)
        self._redraw_job = self.root.after(30, self.recompute)

    def auto_threshold(self):
        if self.an is not None:
            self.vars["threshold_db"].set(engine.auto_threshold(self.an.db))
            self._update_val_label("threshold_db")
            self.recompute()

    def reset_edits(self):
        self.kept_times, self.manual = [], []
        self.recompute()

    def recompute(self):
        self._redraw_job = None
        if self.an is None:
            return
        dur = self.info.duration
        self.silences = engine.detect_silences(self.an.db, dur, self.params())
        kt = sorted(self.kept_times)
        self.silence_on = []
        for a, b in self.silences:
            i = bisect.bisect_left(kt, a)
            self.silence_on.append(not (i < len(kt) and kt[i] <= b))
        cuts = [s for s, on in zip(self.silences, self.silence_on) if on] + self.manual
        self.cuts = engine.merge(cuts)
        self.keeps = engine.keeps_from_cuts(self.cuts, dur)
        new = sum(b - a for a, b in self.keeps)
        saved = dur - new
        self.pct_lbl.configure(text=f"−{saved / dur * 100:.0f}%  shorter" if dur else "–")
        self.len_lbl.configure(text=f"{fmt_time(dur)}  →  {fmt_time(new)}   ({fmt_time(saved)} removed)")
        n_off = self.silence_on.count(False)
        extra = f"   ·   {n_off} kept by hand" if n_off else ""
        extra += f"   ·   {len(self.manual)} cut by hand" if self.manual else ""
        self.cut_lbl.configure(text=f"{len(self.cuts)} cuts{extra}")
        self._update_buttons()
        self.redraw()

    # ------------------------------------------------------------------ view / drawing
    def _t2x(self, t):
        t0, t1 = self.view
        return (t - t0) / (t1 - t0) * self.canvas.winfo_width()

    def _x2t(self, x):
        t0, t1 = self.view
        return t0 + x / max(self.canvas.winfo_width(), 1) * (t1 - t0)

    def _set_view(self, t0, length):
        dur = self.info.duration if self.info else 1.0
        length = min(max(length, 0.5), dur)
        t0 = min(max(t0, 0.0), dur - length)
        self.view = [t0, t0 + length]
        self.redraw()

    def _on_scrollbar(self, *args):
        if not self.info:
            return
        t0, t1 = self.view
        length = t1 - t0
        if args[0] == "moveto":
            self._set_view(float(args[1]) * self.info.duration, length)
        elif args[0] == "scroll":
            step = length * (0.9 if args[2] == "pages" else 0.1)
            self._set_view(t0 + int(args[1]) * step, length)

    def _on_wheel(self, e, pan=False, delta=None):
        if not self.info:
            return
        d = delta if delta is not None else e.delta
        t0, t1 = self.view
        length = t1 - t0
        if pan:
            self._set_view(t0 - d / 120 * length * 0.15, length)
            return
        f = 0.8 ** (d / 120)
        anchor = self._x2t(e.x)
        new_len = length * f
        self._set_view(anchor - (anchor - t0) * (new_len / length), new_len)

    def redraw(self):
        c = self.canvas
        c.delete("all")
        W, H = c.winfo_width(), c.winfo_height()
        if W < 10 or H < 10:
            return
        if self.an is None:
            c.create_text(W / 2, H / 2, fill=C["dim"], font=("Segoe UI", 13),
                          text="Open a video or audio file to find and remove its silences.")
            self.hbar.set(0, 1)
            return
        dur = self.info.duration
        t0, t1 = self.view
        self.hbar.set(t0 / dur, t1 / dur)
        top, bot = RULER + 6, H - 6
        mid, half = (top + bot) / 2, (bot - top) / 2

        # ruler
        span = t1 - t0
        step = next((s for s in (0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900,
                                 1800, 3600) if s / span * W >= 90), 7200)
        t = (t0 // step) * step
        dec = 2 if step < 0.1 else 1 if step < 1 else 0
        while t <= t1:
            x = self._t2x(t)
            c.create_line(x, RULER - 6, x, H, fill=C["grid"])
            c.create_text(x + 4, 3, anchor="nw", fill=C["dim"], font=("Segoe UI", 8),
                          text=fmt_time(t, dec))
            t += step

        # silence regions
        i = max(bisect.bisect_left(self.silences, (t0, t0)) - 1, 0)
        for (a, b), on in zip(self.silences[i:], self.silence_on[i:]):
            if a > t1:
                break
            if b < t0:
                continue
            xa, xb = self._t2x(a), max(self._t2x(b), self._t2x(a) + 1)
            if on:
                c.create_rectangle(xa, top, xb, bot, fill=C["cut"], outline="")
            else:
                c.create_rectangle(xa, top + 1, xb, bot - 1, outline=C["cut_off"], dash=(3, 3))
        for a, b in self.manual:
            if b >= t0 and a <= t1:
                c.create_rectangle(self._t2x(a), top, self._t2x(b), bot, fill=C["manual"], outline="")

        # waveform (loudness envelope), grey where it will be cut
        db = self.an.db
        i0, i1 = int(t0 / HOP), min(int(np.ceil(t1 / HOP)) + 1, len(db))
        seg = db[i0:i1]
        if len(seg):
            cols = W
            if len(seg) >= cols:
                edges = np.linspace(0, len(seg), cols + 1).astype(int)[:-1]
                vals = np.maximum.reduceat(seg, edges)
            else:
                idx = np.minimum((np.arange(cols) * len(seg) / cols).astype(int), len(seg) - 1)
                vals = seg[idx]
            amp = np.clip((vals - VIEW_FLOOR_DB) / -VIEW_FLOOR_DB, 0.0, 1.0) * half * 0.96 + 0.5
            times = t0 + (np.arange(cols) + 0.5) / cols * span
            if self.cuts:
                ca = np.array([a for a, _ in self.cuts])
                cb = np.array([b for _, b in self.cuts])
                k = np.searchsorted(ca, times, side="right") - 1
                cut = (k >= 0) & (times < cb[np.maximum(k, 0)])
            else:
                cut = np.zeros(cols, bool)
            d = np.diff(np.concatenate(([-1], cut.astype(np.int8), [-1])))
            starts = np.flatnonzero(d != 0)[:-1]
            bounds = list(starts) + [cols]
            for s, e in zip(bounds[:-1], bounds[1:]):
                xs = np.arange(s, min(e + 1, cols))
                ys = amp[s:min(e + 1, cols)]
                if len(xs) == 1:
                    xs, ys = np.array([xs[0], xs[0] + 1]), np.repeat(ys, 2)
                pts = np.concatenate([np.column_stack([xs, mid - ys]).ravel(),
                                      np.column_stack([xs[::-1], mid + ys[::-1]]).ravel()])
                c.create_polygon(*pts.tolist(), fill=C["wave_cut"] if cut[s] else C["wave"], outline="")

        # threshold
        thr = self.vars["threshold_db"].get()
        off = np.clip((thr - VIEW_FLOOR_DB) / -VIEW_FLOOR_DB, 0, 1) * half * 0.96
        for y in (mid - off, mid + off):
            c.create_line(0, y, W, y, fill=C["thr"], dash=(6, 4))
        c.create_text(W - 6, mid - off - 3, anchor="se", fill=C["thr"], font=("Segoe UI", 8),
                      text=f"{thr:.1f} dB")

        # playhead
        x = self._t2x(self.playhead)
        c.create_line(x, RULER - 6, x, H, fill=C["head"], width=1, tags="head")
        self._update_time_label()

    def _update_time_label(self):
        if self.info:
            out = self._src_to_out(self.playhead)
            self.time_lbl.configure(text=f"{fmt_time(self.playhead, 2)}   (in result: {fmt_time(out, 2)})")

    def _src_to_out(self, t):
        out = 0.0
        for a, b in self.keeps:
            if t <= a:
                break
            out += min(t, b) - a
        return out

    def _set_playhead(self, t, follow=False):
        if not self.info:
            return
        self.playhead = min(max(t, 0.0), self.info.duration)
        t0, t1 = self.view
        if follow and not (t0 <= self.playhead <= t1):
            self._set_view(self.playhead - (t1 - t0) * 0.1, t1 - t0)
            return
        x = self._t2x(self.playhead)
        self.canvas.coords("head", x, RULER - 6, x, self.canvas.winfo_height())
        self._update_time_label()

    # ------------------------------------------------------------------ mouse editing
    def _region_at(self, t, regions):
        for i, (a, b) in enumerate(regions):
            if a <= t <= b:
                return i
        return None

    def _on_press(self, e):
        if self.an is None:
            return
        self._press = e.x

    def _on_drag(self, e):
        if self._press is None or abs(e.x - self._press) < 5:
            return
        self.canvas.delete("drag")
        H = self.canvas.winfo_height()
        self.canvas.create_rectangle(self._press, RULER + 6, e.x, H - 6, fill="", outline=C["thr"],
                                     width=2, tags="drag")

    def _on_release(self, e):
        if self._press is None:
            return
        x0, self._press = self._press, None
        self.canvas.delete("drag")
        if abs(e.x - x0) >= 5:  # drag = manual cut
            a, b = sorted((self._x2t(x0), self._x2t(e.x)))
            a, b = max(a, 0.0), min(b, self.info.duration)
            if b - a > 0.02:
                self.stop_play()
                self.manual.append((a, b))
                self.recompute()
            return
        t = self._x2t(e.x)
        i = self._region_at(t, self.silences)
        if i is not None:
            self.stop_play()
            a, b = self.silences[i]
            if self.silence_on[i]:
                self.kept_times.append((a + b) / 2)
            else:
                self.kept_times = [k for k in self.kept_times if not a <= k <= b]
            self.recompute()
        else:
            self._set_playhead(t)

    def _on_right_click(self, e):
        if self.an is None:
            return
        i = self._region_at(self._x2t(e.x), self.manual)
        if i is not None:
            self.stop_play()
            del self.manual[i]
            self.recompute()

    # ------------------------------------------------------------------ preview playback
    def _on_space(self, e):
        if isinstance(self.root.focus_get(), ttk.Combobox):
            return
        self.toggle_play()
        return "break"

    def toggle_play(self):
        if self.play_state:
            self.stop_play()
        else:
            self.start_play()

    def start_play(self):
        if self.an is None or winsound is None or self.busy:
            return
        start = self.playhead
        if start >= self.info.duration - 0.05:
            start = 0.0
        samples, sr, fade = self.an.samples, ANALYSIS_SR, 80
        parts, mapping, out = [], [], 0.0
        for a, b in self.keeps:
            if b <= start:
                continue
            a = max(a, start)
            seg = samples[int(a * sr):int(b * sr)].astype(np.float32)
            if len(seg) > 2 * fade:
                ramp = np.linspace(0, 1, fade, dtype=np.float32)
                seg[:fade] *= ramp
                seg[-fade:] *= ramp[::-1]
            parts.append(seg.astype(np.int16))
            mapping.append((out, a))
            out += len(seg) / sr
            if out >= PREVIEW_MAX:
                break
        if not parts:
            return
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="freecut_preview_")
        os.close(fd)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(np.concatenate(parts).tobytes())
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        self.play_state = (time.perf_counter(), mapping, out, path)
        self.btn_play.configure(text="■  Stop")
        self._tick()

    def _tick(self):
        if not self.play_state:
            return
        t_start, mapping, total, _ = self.play_state
        el = time.perf_counter() - t_start
        if el >= total:
            self.stop_play()
            return
        i = bisect.bisect_right([m[0] for m in mapping], el) - 1
        out0, src0 = mapping[max(i, 0)]
        self._set_playhead(src0 + (el - out0), follow=True)
        self.root.after(30, self._tick)

    def stop_play(self):
        if not self.play_state:
            return
        path = self.play_state[3]
        self.play_state = None
        if winsound:
            winsound.PlaySound(None, 0)
        try:
            os.remove(path)
        except OSError:
            pass
        self.btn_play.configure(text="▶  Preview")

    # ------------------------------------------------------------------ export
    def _default_name(self, ext):
        p = Path(self.info.path)
        return f"{p.stem}_freecut{ext}"

    def export_media(self):
        if self.an is None or self.busy:
            return
        self.stop_play()
        src_ext = Path(self.info.path).suffix.lower()
        if self.info.has_video:
            ext = src_ext if src_ext in (".mp4", ".mov", ".mkv", ".webm") else ".mp4"
            types = [("MP4 video", "*.mp4"), ("QuickTime", "*.mov"), ("Matroska", "*.mkv"),
                     ("Audio only (MP3)", "*.mp3"), ("Audio only (WAV)", "*.wav")]
        else:
            ext = src_ext if src_ext in engine.AUDIO_CODECS else ".m4a"
            types = [("MP3", "*.mp3"), ("WAV", "*.wav"), ("M4A (AAC)", "*.m4a"), ("FLAC", "*.flac")]
        types.sort(key=lambda t: t[1] != f"*{ext}")
        out = filedialog.asksaveasfilename(parent=self.root, title="Export", filetypes=types,
                                           initialfile=self._default_name(ext),
                                           defaultextension=ext,
                                           initialdir=str(Path(self.info.path).parent))
        if not out:
            return
        if os.path.abspath(out) == os.path.abspath(self.info.path):
            messagebox.showerror("Freecut", "Pick a different file name — that's the original.")
            return
        info, keeps, q, sp = self.info, list(self.keeps), self.quality.get(), self.speed.get()

        def job():
            t = time.perf_counter()
            engine.render(info, keeps, out, q, sp, lambda p: self.msgs.put(("progress", p)),
                          lambda: self.cancel_flag)
            self.msgs.put(("status", f"Exported {Path(out).name} in {time.perf_counter() - t:.0f} s."))
            self.msgs.put(("exported", out))
        self._run_bg(f"Rendering {Path(out).name}…", job)

    def export_timeline(self, kind):
        if self.an is None or self.busy:
            return
        ext = "." + kind
        label = "Premiere Pro / DaVinci Resolve XML" if kind == "xml" else "Final Cut Pro / Resolve FCPXML"
        out = filedialog.asksaveasfilename(parent=self.root, title=f"Export {label}",
                                           filetypes=[(label, f"*{ext}")],
                                           initialfile=self._default_name(ext), defaultextension=ext,
                                           initialdir=str(Path(self.info.path).parent))
        if not out:
            return
        name = f"{Path(self.info.path).stem} (silences removed)"
        try:
            fn = engine.export_premiere_xml if kind == "xml" else engine.export_fcpxml
            fn(self.info, self.keeps, out, name)
        except Exception as e:
            messagebox.showerror("Freecut", str(e))
            return
        self._set_status(f"Wrote {Path(out).name}. Import it in your editor "
                         f"({'File › Import' if kind == 'xml' else 'File › Import › XML'}).")
        self._on_exported(out)

    def _on_exported(self, path):
        if messagebox.askyesno("Export finished", f"Saved {Path(path).name}.\n\nShow it in Explorer?",
                               parent=self.root):
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                subprocess.Popen(["xdg-open", str(Path(path).parent)])


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    app = App(root)
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        root.after(200, lambda: app.open_file(sys.argv[1]))
    root.mainloop()


if __name__ == "__main__":
    main()
