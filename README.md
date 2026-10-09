# Freecut

Free silence remover for video and audio (a Recut alternative).

- Detects silences (threshold, minimum length, padding, blip filter, auto threshold)
- Shows every audio track of the file; tick/untick a track to include or ignore it for detection
- Preview the edited result with video and sound, cuts skipped
- Click silences to keep them, drag to cut by hand
- Export a rendered video/audio file (all audio tracks kept), or a timeline for
  Premiere / Resolve (`.xml`) or Final Cut / Resolve (`.fcpxml`)

## Run from source

    pip install -r requirements.txt
    python -m freecut

or double-click `Freecut.pyw`.

Command line (no window):

    python -m freecut.cli talk.mp4 -o talk_cut.mp4              # rendered video
    python -m freecut.cli talk.mp4 -o talk.xml                  # Premiere / Resolve timeline
    python -m freecut.cli talk.mkv --tracks 1 -o talk_cut.mp4   # detect on track 1 only

## Build the .exe

    pip install pyinstaller
    python build.py              # dist/Freecut/Freecut.exe (zip the dist/Freecut folder to share)
    python build.py --onefile    # dist/Freecut.exe (single file, slower to start)

The exe includes Python and ffmpeg; the target PC needs nothing installed.
