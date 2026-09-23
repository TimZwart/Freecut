"""Command-line silence remover.

    python -m freecut.cli talk.mp4 -o talk_cut.mp4
    python -m freecut.cli talk.mp4 -o talk.xml          (Premiere / Resolve timeline)
    python -m freecut.cli talk.mp4 -o talk.fcpxml       (Final Cut / Resolve timeline)
"""
import argparse
import sys
from pathlib import Path

from . import engine


def main(argv=None):
    ap = argparse.ArgumentParser(prog="freecut", description="Remove silences from video/audio.")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", help="output file (.mp4/.mov/.mkv/.mp3/.wav/.xml/.fcpxml ...)")
    ap.add_argument("-t", "--threshold", type=float, help="silence threshold in dBFS (default: auto)")
    ap.add_argument("--min-silence", type=float, default=0.5)
    ap.add_argument("--pad-before", type=float, default=0.10)
    ap.add_argument("--pad-after", type=float, default=0.15)
    ap.add_argument("--min-sound", type=float, default=0.05)
    ap.add_argument("--quality", choices=list(engine.VIDEO_QUALITY), default="High")
    ap.add_argument("--speed", default="medium", help="x264 preset (ultrafast ... veryslow)")
    ap.add_argument("--tracks", help="audio tracks to detect silence on, e.g. 1,3 (default: all)")
    a = ap.parse_args(argv)

    def bar(p):
        print(f"\r  {p * 100:5.1f}%", end="", flush=True)

    print(f"Analysing {a.input}")
    info, an = engine.load(a.input, bar)
    print()
    for st in info.audio:
        print("  " + st.label.replace("·", "-"))
    on = [True] * len(info.audio)
    if a.tracks:
        picked = {int(x) - 1 for x in a.tracks.split(",")}
        on = [i in picked for i in range(len(info.audio))]
    db = an.combined(on)
    thr = a.threshold if a.threshold is not None else engine.auto_threshold(db)
    params = engine.Params(thr, a.min_silence, a.pad_before, a.pad_after, a.min_sound)
    cuts = engine.detect_silences(db, info.duration, params)
    keeps = engine.keeps_from_cuts(cuts, info.duration)
    new_len = sum(b - a for a, b in keeps)
    print(f"Threshold {thr:.1f} dB: {len(cuts)} silences, "
          f"{engine.fmt_time(info.duration)} -> {engine.fmt_time(new_len)}")

    if not a.output:
        for s, e in cuts:
            print(f"  cut {engine.fmt_time(s, 2)} - {engine.fmt_time(e, 2)}")
        return
    ext = Path(a.output).suffix.lower()
    name = Path(a.input).stem + " (silences removed)"
    if ext == ".xml":
        engine.export_premiere_xml(info, keeps, a.output, name)
    elif ext == ".fcpxml":
        engine.export_fcpxml(info, keeps, a.output, name)
    else:
        engine.render(info, keeps, a.output, a.quality, a.speed, bar)
        print()
    print(f"Wrote {a.output}")


if __name__ == "__main__":
    sys.exit(main())
