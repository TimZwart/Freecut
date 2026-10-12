"""Build a standalone QuietCut.exe with PyInstaller.

    python build.py             -> dist/QuietCut/QuietCut.exe  (folder, starts fast; zip it to share)
    python build.py --onefile   -> dist/QuietCut.exe          (single file, slower first start)
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ICON = ROOT / "assets" / "quietcut.ico"


def make_icon():
    from PIL import Image, ImageDraw

    s = 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, s - 8, s - 8), radius=52, fill=(47, 111, 214))
    bars = [0.35, 0.7, 0.5, 0.9, 0.12, 0.1, 0.12, 0.8, 0.55, 0.75, 0.4]
    w = 14
    x0 = (s - len(bars) * (w + 6)) // 2
    for i, h in enumerate(bars):
        x = x0 + i * (w + 6)
        half = h * 80
        cut = 4 <= i <= 6
        d.rounded_rectangle((x, s / 2 - half, x + w, s / 2 + half), radius=6,
                            fill=(242, 193, 78) if cut else (255, 255, 255))
    ICON.parent.mkdir(exist_ok=True)
    img.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def main():
    if not ICON.exists():
        make_icon()
    onefile = "--onefile" in sys.argv
    # A running QuietCut.exe locks dist/QuietCut; PyInstaller would delete half of it and
    # then fail, breaking the running copy. Refuse up front instead.
    target = ROOT / "dist" / "QuietCut"
    if not onefile and target.exists():
        try:
            target.rename(target)  # fails while any file inside is in use
        except OSError:
            sys.exit("dist/QuietCut is in use. Close QuietCut.exe and run the build again.")
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", "QuietCut", "--icon", str(ICON),
        "--add-data", f"{ICON}{os.pathsep}assets",
        "--collect-all", "imageio_ffmpeg",     # bundles ffmpeg.exe
        "--onefile" if onefile else "--onedir",
        "QuietCut.pyw",
    ], cwd=ROOT)
    out = ROOT / "dist" / ("QuietCut.exe" if onefile else "QuietCut/QuietCut.exe")
    print(f"\nBuilt {out}")


if __name__ == "__main__":
    main()
