"""Freecut engine: probing, silence detection, rendering and NLE timeline exports.

Everything here is GUI-free so it can be used from the command line (see cli.py)
or from the Tk app (app.py).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote

import numpy as np

ANALYSIS_SR = 16000          # mono sample rate used for loudness analysis
HOP = 0.01                   # seconds per loudness window
DB_FLOOR = -100.0
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class Cancelled(Exception):
    pass


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        found = shutil.which("ffmpeg")
        if found:
            return found
        raise RuntimeError("ffmpeg not found. Install it with:  pip install imageio-ffmpeg")


def popen(cmd, **kw) -> subprocess.Popen:
    """Popen that never flashes a console window and never inherits stdin."""
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, **kw)


# --------------------------------------------------------------------------- probing

@dataclass
class AudioStream:
    index: int               # n-th audio stream (0-based), i.e. ffmpeg's 0:a:<index>
    channels: int = 2
    sample_rate: int = 48000
    title: str = ""

    @property
    def label(self) -> str:
        layout = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(self.channels, f"{self.channels} ch")
        name = f"Track {self.index + 1}"
        return f"{name} · {self.title} · {layout}" if self.title else f"{name} · {layout}"


@dataclass
class MediaInfo:
    path: str
    duration: float
    has_video: bool
    width: int = 0
    height: int = 0
    fps: Fraction | None = None
    audio: list[AudioStream] = field(default_factory=list)
    timecode: str | None = None    # embedded start timecode, e.g. "01:00:00:00"
    start_time: float = 0.0        # container start; stream timestamps are relative to this

    @property
    def timeline_fps(self) -> Fraction:
        return self.fps or Fraction(30)

    @property
    def sample_rate(self) -> int:
        return self.audio[0].sample_rate

    @property
    def channels(self) -> int:
        return self.audio[0].channels


def _to_rate(f: float) -> Fraction:
    if abs(f - round(f)) < 0.01:
        return Fraction(round(f))
    ntsc = f * 1.001
    if abs(ntsc - round(ntsc)) < 0.01:
        return Fraction(round(ntsc) * 1000, 1001)
    return Fraction(f).limit_denominator(1001)


def _channels(line: str) -> int:
    for key, n in (("mono", 1), ("stereo", 2), ("5.1", 6), ("7.1", 8), ("quad", 4)):
        if key in line:
            return n
    r = re.search(r"(\d+) channels", line)
    return int(r[1]) if r else 2


def probe(path: str) -> MediaInfo:
    res = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path], capture_output=True,
                         stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                         creationflags=NO_WINDOW)
    out = res.stderr
    if "Invalid data found" in out or "No such file" in out:
        raise RuntimeError(f"ffmpeg can't read this file:\n{out.strip().splitlines()[-1]}")

    duration = 0.0
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if m:
        duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])

    info = MediaInfo(path=path, duration=duration, has_video=False)
    m = re.search(r"Duration: .*?start: (-?[\d.]+)", out)
    if m:
        info.start_time = float(m[1])
    current: AudioStream | None = None   # stream whose metadata lines we're reading
    for line in out.splitlines():
        if re.search(r"Stream #\S+", line):
            current = None
            if re.search(r": Video:", line) and "attached pic" not in line and not info.has_video:
                info.has_video = True
                r = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line)
                if r:
                    info.width, info.height = int(r[1]), int(r[2])
                r = re.search(r"([\d.]+) fps", line) or re.search(r"([\d.]+)k? tbr", line)
                info.fps = _to_rate(float(r[1])) if r else Fraction(30)
            elif re.search(r": Audio:", line):
                r = re.search(r"(\d+) Hz", line)
                current = AudioStream(index=len(info.audio), channels=_channels(line),
                                      sample_rate=int(r[1]) if r else 48000)
                info.audio.append(current)
            continue
        r = re.match(r"\s*title\s*:\s*(.+)", line)
        if r and current is not None and not current.title:
            current.title = r[1].strip()
        r = re.match(r"\s*timecode\s*:\s*(\d\d:\d\d:\d\d[:;]\d\d)", line)
        if r and info.timecode is None:
            info.timecode = r[1]
    if not info.audio:
        raise RuntimeError("This file has no audio track, so there is nothing to detect silence in.")
    return info


# --------------------------------------------------------------------------- analysis

@dataclass
class Analysis:
    dbs: list[np.ndarray]    # loudness per HOP window (dBFS), one array per audio track
    duration: float

    def combined(self, enabled: list[bool]) -> np.ndarray:
        """Loudest enabled track per window: silence = every enabled track is quiet."""
        picked = [d for d, on in zip(self.dbs, enabled) if on]
        if not picked:
            return np.full_like(self.dbs[0], DB_FLOOR)
        return np.maximum.reduce(picked) if len(picked) > 1 else picked[0]


def loudness(samples: np.ndarray) -> np.ndarray:
    hop_n = int(ANALYSIS_SR * HOP)
    n = int(np.ceil(len(samples) / hop_n))
    db = np.empty(n, dtype=np.float32)
    block = 10000  # windows per block keeps the float conversion small
    for i in range(0, n, block):
        seg = samples[i * hop_n:(i + block) * hop_n].astype(np.float32) / 32768.0
        pad = (-len(seg)) % hop_n
        if pad:
            seg = np.concatenate([seg, np.zeros(pad, np.float32)])
        rms = np.sqrt(np.mean(seg.reshape(-1, hop_n) ** 2, axis=1))
        db[i:i + len(rms)] = 20 * np.log10(np.maximum(rms, 1e-5))
    return db


def analyze(info: MediaInfo, progress=None, cancel=None) -> Analysis:
    """Decode every audio track in one pass (mono, 16 kHz) and measure loudness."""
    tmp = tempfile.mkdtemp(prefix="freecut_")
    raws = [os.path.join(tmp, f"a{s.index}.raw") for s in info.audio]
    cmd = [ffmpeg_exe(), "-v", "error", "-nostdin", "-y", "-i", info.path]
    for s, raw in zip(info.audio, raws):
        cmd += ["-map", f"0:a:{s.index}", "-ac", "1", "-ar", str(ANALYSIS_SR), "-f", "s16le", raw]
    cmd += ["-progress", "pipe:1", "-nostats"]
    proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            if cancel and cancel():
                proc.kill()
                proc.wait()
                raise Cancelled()
            if line.startswith("out_time_us=") and progress and info.duration > 0:
                try:
                    progress(min(int(line.split("=")[1]) / 1e6 / info.duration, 1.0))
                except ValueError:
                    pass
        err = proc.stderr.read()
        proc.wait()
        dbs = []
        for raw in raws:
            samples = np.fromfile(raw, dtype=np.int16) if os.path.exists(raw) else np.zeros(0, np.int16)
            dbs.append(loudness(samples))
        if not any(len(d) for d in dbs):
            raise RuntimeError(f"Could not decode audio.\n{err.strip()}")
    finally:
        proc.stdout.close()
        proc.stderr.close()
        shutil.rmtree(tmp, ignore_errors=True)
    n = max(len(d) for d in dbs)
    dbs = [np.concatenate([d, np.full(n - len(d), DB_FLOOR, np.float32)]) for d in dbs]
    return Analysis(dbs=dbs, duration=n * HOP)


def load(path: str, progress=None, cancel=None) -> tuple[MediaInfo, Analysis]:
    info = probe(path)
    an = analyze(info, progress, cancel)
    if info.duration <= 0:
        info.duration = an.duration
    return info, an


def auto_threshold(db: np.ndarray) -> float:
    v = db[db > DB_FLOOR + 1]
    if len(v) < 10:
        return -40.0
    floor, loud = np.percentile(v, 10), np.percentile(v, 95)
    thr = floor + max(loud - floor, 6) * 0.3
    return float(np.clip(round(thr * 2) / 2, -70, -10))


def grab_frame(path: str, t: float, w: int, h: int) -> bytes | None:
    """One RGB24 frame at time t scaled to w x h, or None."""
    cmd = [ffmpeg_exe(), "-v", "error", "-nostdin", "-ss", f"{max(t, 0):.3f}", "-i", path,
           "-map", "0:v:0", "-frames:v", "1", "-vf", f"scale={w}:{h}", "-f", "rawvideo",
           "-pix_fmt", "rgb24", "-"]
    res = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    return res.stdout if len(res.stdout) == w * h * 3 else None


# --------------------------------------------------------------------------- detection

@dataclass
class Params:
    threshold_db: float = -40.0
    min_silence: float = 0.5     # silences shorter than this are left alone
    pad_before: float = 0.10     # audio kept before speech resumes
    pad_after: float = 0.15      # audio kept after speech stops
    min_sound: float = 0.05      # louder blips shorter than this count as silence

    def to_dict(self):
        return asdict(self)


def _runs(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Start (inclusive) and end (exclusive) indices of the True runs in mask."""
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return np.flatnonzero(d == 1), np.flatnonzero(d == -1)


def detect_silences(db: np.ndarray, duration: float, p: Params) -> list[tuple[float, float]]:
    """Silent stretches to remove, already shrunk by the padding. Seconds."""
    loud = db >= p.threshold_db
    min_sound_n = int(round(p.min_sound / HOP))
    if min_sound_n > 1:
        s, e = _runs(loud)
        for a, b in zip(s[(e - s) < min_sound_n], e[(e - s) < min_sound_n]):
            loud[a:b] = False
    s, e = _runs(~loud)
    long_enough = (e - s) >= max(1, int(round(p.min_silence / HOP)))
    out = []
    n = len(db)
    for a, b in zip(s[long_enough], e[long_enough]):
        start = 0.0 if a == 0 else a * HOP + p.pad_after
        end = duration if b >= n else min(b * HOP, duration) - p.pad_before
        if end - start > 0.02:
            out.append((start, end))
    return out


def merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def keeps_from_cuts(cuts: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    keeps, t = [], 0.0
    for a, b in merge(cuts):
        a, b = max(a, 0.0), min(b, duration)
        if a > t + 1e-6:
            keeps.append((t, a))
        t = max(t, b)
    if duration > t + 1e-6:
        keeps.append((t, duration))
    return keeps


def keep_frames(keeps: list[tuple[float, float]], fps: Fraction) -> list[tuple[int, int]]:
    """Snap keep ranges to whole frames: list of [first, end) frame indices."""
    out: list[list[int]] = []
    for a, b in keeps:
        fa, fb = round(a * fps), round(b * fps)
        if fb <= fa:
            continue
        if out and fa <= out[-1][1]:
            out[-1][1] = max(out[-1][1], fb)
        else:
            out.append([fa, fb])
    return [(a, b) for a, b in out]


def fmt_time(t: float, decimals: int = 1) -> str:
    t = max(t, 0.0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    sec = f"{s:0{3 + decimals if decimals else 2}.{decimals}f}"
    return f"{int(h)}:{int(m):02d}:{sec}" if h >= 1 else f"{int(m)}:{sec}"


# --------------------------------------------------------------------------- rendering

VIDEO_QUALITY = {"High": 18, "Balanced": 21, "Small file": 25}
AUDIO_CODECS = {".mp3": ["-c:a", "libmp3lame", "-q:a", "2"], ".wav": ["-c:a", "pcm_s16le"],
                ".flac": ["-c:a", "flac"], ".m4a": ["-c:a", "aac", "-b:a", "192k"],
                ".aac": ["-c:a", "aac", "-b:a", "192k"], ".ogg": ["-c:a", "libvorbis", "-q:a", "6"],
                ".opus": ["-c:a", "libopus", "-b:a", "128k"]}


def _keep_seconds(info: MediaInfo, keeps):
    """Keep ranges as seconds; for video, snapped to whole frames."""
    if info.has_video:
        fps = info.timeline_fps
        return [(float(a / fps), float(b / fps)) for a, b in keep_frames(keeps, fps)], float(1 / fps)
    return merge(keeps), 0.0


def _filter_script(info: MediaInfo, keeps, video: bool, mix_audio: bool) -> tuple[str, list[str]]:
    """Filtergraph plus the output labels to map."""
    ks, frame = _keep_seconds(info, keeps)
    # Video frames sit on the frame grid, so select with half-frame tolerance;
    # audio is selected in small chunks by exact start time.
    half = frame / 2
    vsel = "+".join(f"between(t,{a - half:.6f},{b - half - 1e-6:.6f})" for a, b in ks)
    asel = "+".join(f"between(t,{a:.6f},{b - 1e-6:.6f})" for a, b in ks)
    # Output time = input time minus everything removed before it. Using the same
    # mapping for every stream keeps them locked together no matter how many cuts.
    cuts, prev = [], 0.0
    for a, b in ks:
        if a > prev + 1e-9:
            cuts.append((prev, a - prev))
        prev = b
    shift = "+".join(f"clip(T-{s:.6f},0,{l:.6f})" for s, l in cuts) or "0"
    # Every stream is shifted by the same container start so their relative offsets survive.
    zero = f"PTS-{info.start_time:.6f}/TB"
    parts, labels = [], []
    if video:
        parts.append(f"[0:v:0]setpts={zero},select='{vsel}',setpts='(T-({shift}))/TB',"
                     f"fps={info.timeline_fps}[v]")
        labels.append("[v]")
    alabels = []
    for s in info.audio:
        parts.append(f"[0:a:{s.index}]asetpts={zero},asetnsamples=n=128:p=0,aselect='{asel}',"
                     f"asetpts='(T-({shift}))/TB',aresample=async=1:min_hard_comp=0.001:first_pts=0"
                     f"[a{s.index}]")
        alabels.append(f"[a{s.index}]")
    if mix_audio and len(alabels) > 1:
        parts.append(f"{''.join(alabels)}amix=inputs={len(alabels)}:normalize=0[amix]")
        alabels = ["[amix]"]
    return ";\n".join(parts), labels + alabels


def render(info: MediaInfo, keeps, out_path: str, quality: str = "High", speed: str = "medium",
           progress=None, cancel=None) -> None:
    ks, _ = _keep_seconds(info, keeps)
    if not ks:
        raise RuntimeError("Nothing left to render: every part of the file is marked as cut.")
    total = sum(b - a for a, b in ks)
    ext = Path(out_path).suffix.lower()
    audio_only = ext in AUDIO_CODECS
    video = info.has_video and not audio_only
    graph, labels = _filter_script(info, keeps, video, mix_audio=audio_only)
    fd, script = tempfile.mkstemp(suffix=".txt", prefix="freecut_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(graph)

    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-nostdin", "-i", info.path, "-/filter_complex", script]
    for label in labels:
        cmd += ["-map", label]
    if video:
        cmd += ["-c:v", "libx264", "-preset", speed, "-crf", str(VIDEO_QUALITY.get(quality, 18)),
                "-pix_fmt", "yuv420p"]
        cmd += ["-c:a", "libopus", "-b:a", "160k"] if ext == ".webm" else ["-c:a", "aac", "-b:a", "192k"]
        if ext in (".mp4", ".mov", ".m4v"):
            cmd += ["-movflags", "+faststart"]
        for s in info.audio:  # keep track names
            if s.title:
                cmd += [f"-metadata:s:a:{s.index}", f"title={s.title}"]
    else:
        cmd += AUDIO_CODECS.get(ext, ["-c:a", "aac", "-b:a", "192k"])
    cmd += ["-progress", "pipe:1", "-nostats", out_path]

    err_file = tempfile.TemporaryFile()
    proc = popen(cmd, stdout=subprocess.PIPE, stderr=err_file, text=True)
    try:
        for line in proc.stdout:
            if cancel and cancel():
                proc.kill()
                proc.wait()
                try:
                    os.remove(out_path)
                except OSError:
                    pass
                raise Cancelled()
            if line.startswith("out_time_us=") and progress:
                try:
                    progress(min(int(line.split("=")[1]) / 1e6 / total, 1.0))
                except ValueError:
                    pass
        proc.wait()
        if proc.returncode != 0:
            err_file.seek(0)
            tail = err_file.read().decode("utf-8", "replace").strip().splitlines()[-15:]
            raise RuntimeError("ffmpeg failed:\n" + "\n".join(tail))
    finally:
        proc.stdout.close()
        err_file.close()
        try:
            os.remove(script)
        except OSError:
            pass


# --------------------------------------------------------------------------- timeline exports

def _tc_to_frames(tc: str | None, fps: Fraction) -> int:
    if not tc:
        return 0
    h, m, s, f = map(int, re.split(r"[:;.]", tc))
    base = round(fps)
    if ";" in tc and fps.denominator == 1001:  # drop-frame
        drop = 2 * base // 30
        total_min = 60 * h + m
        return (base * 3600 * h + base * 60 * m + base * s + f) - drop * (total_min - total_min // 10)
    return (h * 3600 + m * 60 + s) * base + f


def _file_url(path: str, fcpxml: bool) -> str:
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return ("file://" if fcpxml else "file://localhost") + quote(p, safe="/:")


def _sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def _write_xml(root, out_path: str, doctype: str):
    ET.indent(root, "  ")
    body = ET.tostring(root, encoding="unicode")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE {doctype}>\n{body}\n')


def export_premiere_xml(info: MediaInfo, keeps, out_path: str, name: str) -> None:
    """Final Cut Pro 7 XML (xmeml): imports into Premiere Pro and DaVinci Resolve."""
    fps = info.timeline_fps
    frames = keep_frames(keeps, fps)
    src_len = round(info.duration * fps)
    tl_len = sum(b - a for a, b in frames)
    clip_name = Path(info.path).name
    # One timeline track per source audio track; a single stereo track becomes L + R.
    if len(info.audio) == 1:
        a_tracks = list(range(1, min(info.channels, 2) + 1))
    else:
        a_tracks = [s.index + 1 for s in info.audio]

    def rate(parent):
        r = _sub(parent, "rate")
        _sub(r, "timebase", round(fps))
        _sub(r, "ntsc", "TRUE" if fps.denominator == 1001 else "FALSE")

    def timecode(parent, frame):
        tc = _sub(parent, "timecode")
        rate(tc)
        _sub(tc, "frame", frame)
        _sub(tc, "displayformat", "NDF")

    root = ET.Element("xmeml", version="4")
    seq = _sub(root, "sequence", id="sequence-1")
    _sub(seq, "name", name)
    _sub(seq, "duration", tl_len)
    rate(seq)
    timecode(seq, 0)
    media = _sub(seq, "media")
    video = _sub(media, "video")
    if info.has_video:
        sc = _sub(_sub(video, "format"), "samplecharacteristics")
        rate(sc)
        _sub(sc, "width", info.width)
        _sub(sc, "height", info.height)
        _sub(sc, "pixelaspectratio", "square")
        _sub(sc, "anamorphic", "FALSE")
        _sub(sc, "fielddominance", "none")
    audio = _sub(media, "audio")
    _sub(audio, "numOutputChannels", 2)
    asc = _sub(_sub(audio, "format"), "samplecharacteristics")
    _sub(asc, "depth", 16)
    _sub(asc, "samplerate", info.sample_rate)

    v_track = _sub(video, "track") if info.has_video else None
    a_track_els = {idx: _sub(audio, "track") for idx in a_tracks}
    file_written = False

    def file_el(parent):
        nonlocal file_written
        if file_written:
            return _sub(parent, "file", id="file-1")
        file_written = True
        f = _sub(parent, "file", id="file-1")
        _sub(f, "name", clip_name)
        _sub(f, "pathurl", _file_url(info.path, False))
        rate(f)
        _sub(f, "duration", src_len)
        timecode(f, _tc_to_frames(info.timecode, fps))
        m = _sub(f, "media")
        if info.has_video:
            vsc = _sub(_sub(m, "video"), "samplecharacteristics")
            rate(vsc)
            _sub(vsc, "width", info.width)
            _sub(vsc, "height", info.height)
        am = _sub(m, "audio")
        s = _sub(am, "samplecharacteristics")
        _sub(s, "depth", 16)
        _sub(s, "samplerate", info.sample_rate)
        _sub(am, "channelcount", len(a_tracks))
        return f

    tl = 0
    for i, (fa, fb) in enumerate(frames, 1):
        ids = ([("video", 1, f"clipitem-v{i}")] if info.has_video else []) + \
              [("audio", idx, f"clipitem-a{idx}-{i}") for idx in a_tracks]
        for kind, idx, cid in ids:
            track = v_track if kind == "video" else a_track_els[idx]
            ci = _sub(track, "clipitem", id=cid)
            _sub(ci, "name", clip_name)
            _sub(ci, "enabled", "TRUE")
            _sub(ci, "duration", src_len)
            rate(ci)
            _sub(ci, "start", tl)
            _sub(ci, "end", tl + fb - fa)
            _sub(ci, "in", fa)
            _sub(ci, "out", fb)
            file_el(ci)
            if kind == "audio":
                st = _sub(ci, "sourcetrack")
                _sub(st, "mediatype", "audio")
                _sub(st, "trackindex", idx)
            for k2, idx2, cid2 in ids:
                ln = _sub(ci, "link")
                _sub(ln, "linkclipref", cid2)
                _sub(ln, "mediatype", k2)
                _sub(ln, "trackindex", idx2)
                _sub(ln, "clipindex", i)
        tl += fb - fa
    _write_xml(root, out_path, "xmeml")


def export_fcpxml(info: MediaInfo, keeps, out_path: str, name: str) -> None:
    """FCPXML 1.9: imports into Final Cut Pro and DaVinci Resolve."""
    fps = info.timeline_fps
    frames = keep_frames(keeps, fps)

    def t(n_frames: int) -> str:
        v = Fraction(n_frames) / fps
        return f"{v.numerator}s" if v.denominator == 1 else f"{n_frames * fps.denominator}/{fps.numerator}s"

    tc0 = _tc_to_frames(info.timecode, fps)
    src_len = round(info.duration * fps)
    w, h = (info.width, info.height) if info.has_video else (1920, 1080)
    audio_rate = {32000: "32k", 44100: "44.1k", 48000: "48k", 88200: "88.2k", 96000: "96k"}.get(
        info.sample_rate, "48k")

    root = ET.Element("fcpxml", version="1.9")
    res = _sub(root, "resources")
    _sub(res, "format", id="r1", frameDuration=t(1), width=w, height=h)
    _sub(res, "asset", id="r2", name=Path(info.path).stem, start=t(tc0), duration=t(src_len),
         hasVideo=int(info.has_video), hasAudio=1, format="r1", audioSources=len(info.audio),
         audioChannels=info.channels, audioRate=info.sample_rate, src=_file_url(info.path, True))
    event = _sub(_sub(root, "library"), "event", name="Freecut")
    project = _sub(event, "project", name=name)
    tl_len = sum(b - a for a, b in frames)
    seq = _sub(project, "sequence", format="r1", duration=t(tl_len), tcStart="0s", tcFormat="NDF",
               audioLayout="stereo" if info.channels >= 2 else "mono", audioRate=audio_rate)
    spine = _sub(seq, "spine")
    tl = 0
    for fa, fb in frames:
        _sub(spine, "asset-clip", ref="r2", name=Path(info.path).stem, offset=t(tl),
             start=t(tc0 + fa), duration=t(fb - fa), format="r1", tcFormat="NDF")
        tl += fb - fa
    _write_xml(root, out_path, "fcpxml")
