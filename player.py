"""Preview player: plays the edited result (cuts skipped) with video and all audio tracks.

Two ffmpeg processes stream decoded audio and video from the playhead. Reader threads
drop everything that falls in a cut (or re-seek for long cuts) and queue the rest.
The sound card pulls audio through a sounddevice callback; the number of samples it
has played is the master clock, and the UI shows whichever video frame matches it.
"""
from __future__ import annotations

import queue
import subprocess
import threading
from bisect import bisect_right

import numpy as np
import sounddevice as sd

from .engine import MediaInfo, ffmpeg_exe, popen

SR = 48000
RESEEK_GAP = 3.0      # restart the decoder instead of reading through cuts longer than this
FADE = 144            # 3 ms fades at every cut to avoid clicks
_END = object()


class Player:
    def __init__(self, info: MediaInfo, keeps: list[tuple[float, float]], start: float,
                 video_size: tuple[int, int] | None, max_len: float = 3600.0):
        self.info = info
        self.video_size = video_size if info.has_video else None
        self.segs: list[tuple[float, float]] = []
        self.out_starts: list[float] = []
        out = 0.0
        for a, b in keeps:
            if b <= start:
                continue
            a = max(a, start)
            self.segs.append((a, b))
            self.out_starts.append(out)
            out += b - a
            if out >= max_len:
                break
        self.total = out
        self.stop_ev = threading.Event()
        self.audio_q: queue.Queue = queue.Queue(maxsize=48)   # ~2 s of 2048-sample chunks
        self.video_q: queue.Queue = queue.Queue(maxsize=10)
        self._buf, self._pos = None, 0
        self.played = 0
        self.audio_done = False
        self._pending = None
        self._procs: list[subprocess.Popen] = []
        self.stream = None

    # ------------------------------------------------------------------ control
    def start(self):
        if not self.segs:
            self.audio_done = True
            return
        threading.Thread(target=self._read, daemon=True, args=(
            self._audio_cmd, 4, SR, 2048, self._deliver_audio, self.audio_q)).start()
        if self.video_size:
            w, h = self.video_size
            fps = float(self.info.timeline_fps)
            threading.Thread(target=self._read, daemon=True, args=(
                self._video_cmd, w * h * 3, fps, 1, self._deliver_video, self.video_q)).start()
        self.stream = sd.OutputStream(samplerate=SR, channels=2, dtype="int16",
                                      callback=self._callback, latency="low")
        self.stream.start()

    def stop(self):
        self.stop_ev.set()
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        for p in self._procs:
            try:
                p.kill()
            except Exception:
                pass

    @property
    def finished(self) -> bool:
        return self.audio_done

    # ------------------------------------------------------------------ clock
    def out_time(self) -> float:
        lat = self.stream.latency if self.stream is not None else 0.0
        return max(self.played / SR - lat, 0.0)

    def src_time(self) -> float:
        """Position in the source file that is audible right now."""
        if not self.segs:
            return 0.0
        o = self.out_time()
        i = max(bisect_right(self.out_starts, o) - 1, 0)
        a, b = self.segs[i]
        return min(a + (o - self.out_starts[i]), b)

    def frame_for(self, src_t: float):
        """Latest decoded frame at or before src_t: (time, rgb bytes), or None if unchanged."""
        latest = None
        half = 0.5 / float(self.info.timeline_fps)
        while True:
            if self._pending is None:
                try:
                    self._pending = self.video_q.get_nowait()
                except queue.Empty:
                    break
            if self._pending is _END or self._pending[0] > src_t + half:
                break
            latest, self._pending = self._pending, None
        return latest

    # ------------------------------------------------------------------ audio output
    def _callback(self, outdata, frames, _time, _status):
        filled = 0
        while filled < frames:
            if self._buf is None or self._pos >= len(self._buf):
                try:
                    item = self.audio_q.get_nowait()
                except queue.Empty:
                    break
                if item is _END:
                    self.audio_done = True
                    break
                self._buf, self._pos = item, 0
            n = min(frames - filled, len(self._buf) - self._pos)
            outdata[filled:filled + n] = self._buf[self._pos:self._pos + n]
            self._pos += n
            filled += n
        outdata[filled:] = 0
        self.played += filled

    # ------------------------------------------------------------------ decoding
    def _audio_cmd(self, t: float):
        cmd = [ffmpeg_exe(), "-v", "error", "-nostdin", "-ss", f"{t:.4f}", "-i", self.info.path]
        n = len(self.info.audio)
        if n > 1:
            ins = "".join(f"[0:a:{i}]" for i in range(n))
            cmd += ["-filter_complex", f"{ins}amix=inputs={n}:normalize=0[m]", "-map", "[m]"]
        else:
            cmd += ["-map", "0:a:0"]
        return cmd + ["-ac", "2", "-ar", str(SR), "-f", "s16le", "-"]

    def _video_cmd(self, t: float):
        w, h = self.video_size
        return [ffmpeg_exe(), "-v", "error", "-nostdin", "-ss", f"{t:.4f}", "-i", self.info.path,
                "-map", "0:v:0", "-vf", f"fps={self.info.timeline_fps},scale={w}:{h}",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]

    def _put(self, q: queue.Queue, item) -> bool:
        while not self.stop_ev.is_set():
            try:
                q.put(item, timeout=0.1)
                return True
            except queue.Full:
                pass
        return False

    def _deliver_audio(self, data: bytes, t0: float, first: bool, last: bool):
        arr = np.frombuffer(data, dtype=np.int16).reshape(-1, 2)
        if (first or last) and len(arr) > 2 * FADE:
            arr = arr.astype(np.float32)
            ramp = np.linspace(0, 1, FADE, dtype=np.float32)[:, None]
            if first:
                arr[:FADE] *= ramp
            if last:
                arr[-FADE:] *= ramp[::-1]
            arr = arr.astype(np.int16)
        return self._put(self.audio_q, arr)

    def _deliver_video(self, data: bytes, t0: float, first: bool, last: bool):
        return self._put(self.video_q, (t0, data))

    def _read(self, make_cmd, unit_bytes, rate, chunk, deliver, q):
        """Stream the kept segments: skip through short cuts, re-seek over long ones."""
        proc, p_start, units = None, 0.0, 0

        def read_exact(n):
            buf = bytearray()
            while len(buf) < n:
                part = proc.stdout.read(n - len(buf))
                if not part:
                    break
                buf += part
            return bytes(buf)

        try:
            for a, b in self.segs:
                if self.stop_ev.is_set():
                    return
                if proc is None or a - (p_start + units / rate) > RESEEK_GAP:
                    if proc is not None:
                        proc.kill()
                    proc = popen(make_cmd(a), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 bufsize=1 << 20)
                    self._procs.append(proc)
                    p_start, units = a, 0
                ua, ub = round((a - p_start) * rate), round((b - p_start) * rate)
                skip = ua - units
                skip_chunk = max(1, (1 << 20) // unit_bytes)
                while skip > 0:  # read through the cut
                    got = len(read_exact(min(skip, skip_chunk) * unit_bytes)) // unit_bytes
                    if got == 0:
                        return
                    skip -= got
                    units += got
                while units < ub:
                    want = min(chunk, ub - units)
                    data = read_exact(want * unit_bytes)
                    got = len(data) // unit_bytes
                    if got == 0:
                        return
                    if not deliver(data[:got * unit_bytes], p_start + units / rate,
                                   units == ua, units + got >= ub):
                        return
                    units += got
                    if got < want:
                        return
        finally:
            if proc is not None:
                proc.kill()
            self._put(q, _END)
