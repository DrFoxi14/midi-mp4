# MIDI-MP4 — local SATB workstation

Local-first workstation for importing SATB scores, inspecting notation, generating separate MIDI tracks, previewing parts with a local synthesizer, and developing synchronized score videos.

**Status: foundation / beta, not production-complete.** This initial build establishes the local score pipeline, MIDI export, browser MusicXML rendering, preview WAV and automated tests. MP4/MOV rendering, exact MuseScore layout preservation, and robust cursor synchronization across a broad corpus still need implementation and cross-score verification.

## Privacy model

- Score files and generated media stay on the machine running the app.
- The application does not call cloud APIs or upload scores to external services.
- The server binds to `127.0.0.1` by default.
- One-time dependency installation uses pip/npm package registries. During normal use, score-processing requests do not call external services.

## Requirements

- Python 3.11+
- Node.js and npm (to install the local MusicXML renderer)
- MuseScore 4 CLI (optional; required to import `.mscz` files)

## Setup on macOS

From the cloned repository directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm install
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765. The app is intended for local use; do not bind it to a public network interface.

If the MuseScore 4 CLI is on your PATH, the app detects it automatically. The standard macOS application location is also checked. You can export a score from MuseScore as MusicXML when the CLI is unavailable.

## Tests

```bash
python -m pytest
```

The test suite covers reference-tempo scaling, tempo changes, MIDI track separation, tie merging, voice-name detection, and a minimal four-part MusicXML fixture.

## Implemented in this foundation

- Import MusicXML `.musicxml`, `.xml`, compressed MusicXML `.mxl`, and MuseScore `.mscz` via a local MuseScore CLI.
- Parse notes, rests, durations, pitches, ties, measures and tempo-mark positions with music21.
- SATB name detection with an order-based fallback for common four-part exports.
- Generate a single multitrack MIDI with independent Soprano, Alto, Tenor and Bass tracks; each voice can also be downloaded separately.
- Set a reference BPM while preserving tempo ratios. Example: source markings 60 and 90 become 80 and 120 at reference 80.
- Generate an offline piano-inspired synthesized WAV preview; it is not yet a sample-based acoustic-piano library.
- Render the normalized MusicXML in the browser using the locally installed OpenSheetMusicDisplay package.
- Apply a global preview offset in milliseconds.
- Keep uploaded files in the local application data directory, configurable with `MIDI_MP4_DATA_DIR`.

## Not yet complete

- MP4/MOV export, H.264/AAC muxing, resolution selection, voice-highlighted videos and isolated/accompaniment video modes.
- Guaranteed exact preservation of MuseScore page engraving and layout. The current renderer uses normalized MusicXML.
- A verified note-by-note cursor mapping across the full range of MusicXML engraving conventions.
- Full handling of repeats, navigation marks, unusual voice assignments, and every notation edge case.
- Validation against a broad corpus of real SATB scores. The included fixture is intentionally small and is not sufficient evidence for production readiness.

These features are deliberately shown as unavailable rather than represented as working. Check `GET /api/health` for local dependency status and video-export status.
