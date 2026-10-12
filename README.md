# QuietCut

Free silence remover for video and audio (a Recut alternative).

- Detects silences (threshold, minimum length, padding, blip filter, auto threshold)
- Shows every audio track of the file; tick/untick a track to include or ignore it for detection
- Preview the edited result with video and sound, cuts skipped
- Click silences to keep them, drag to cut by hand
- Export a rendered video/audio file (all audio tracks kept), or a timeline for
  DaVinci Resolve (`.otio`), Premiere Pro (`.xml`) or Final Cut Pro (`.fcpxml`)

For DaVinci Resolve use the `.otio` export (File › Import › Timeline): it puts every audio
track of a multi-track recording (e.g. OBS) on its own timeline track. Resolve's FCP7 `.xml`
import only ever brings in the first audio track, whatever the file says.

## Run from source

    pip install -r requirements.txt
    python -m quietcut

or double-click `QuietCut.pyw`.

Command line (no window):

    python -m quietcut.cli talk.mp4 -o talk_cut.mp4              # rendered video
    python -m quietcut.cli talk.mp4 -o talk.otio                 # DaVinci Resolve timeline
    python -m quietcut.cli talk.mp4 -o talk.xml                  # Premiere Pro timeline
    python -m quietcut.cli talk.mkv --tracks 1 -o talk_cut.mp4   # detect on track 1 only

## Build the .exe

    pip install pyinstaller
    python build.py              # dist/QuietCut/QuietCut.exe (zip the dist/QuietCut folder to share)
    python build.py --onefile    # dist/QuietCut.exe (single file, slower to start)

The exe includes Python and ffmpeg; the target PC needs nothing installed.
