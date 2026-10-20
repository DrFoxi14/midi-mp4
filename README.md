# MIDI-MP4 — local SATB workstation

A local-first workstation for importing SATB scores, inspecting note events, producing per-voice MIDI, previewing the music locally, and preparing synced score videos.

> **Project status: active foundation / beta.** The repository starts from empty. This first implementation establishes a local API, score-ingestion pipeline, MIDI generation, synthetic local audio preview, a browser interface, and automated tests. Faithful MuseScore page rendering and production-grade MP4/MOV export are staged features, not claimed as complete in this commit.

## Privacy

- Score files are processed on the machine running this app.
- No cloud API is called and no score is uploaded anywhere by the application.
- The development server binds to `127.0.0.1` by default.
- Dependencies are installed during setup; normal score-processing requests do not call external services.

## Requirements

- Python 3.11+
- MuseScore 4 CLI (optional, for importing `.mscz` files)
- FFmpeg (optional; required for future/extended video rendering)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765.

For `.mscz` input, install MuseScore 4 and configure `MUSESCORE_BIN` to its CLI executable if it is not detected automatically.

## Development and tests

```bash
python -m pytest
```

## Current capabilities

- MusicXML `.musicxml`, `.xml`, compressed MusicXML `.mxl`, and MuseScore `.mscz` ingestion (the latter needs the local MuseScore CLI).
- SATB part detection with a manual mapping fallback.
- Note/rest events with quarter-note positions, durations, MIDI pitches, ties, and tempo-mark positions.
- Four independent MIDI tracks, with configurable reference tempo while preserving tempo-mark ratios.
- Local WAV preview generated from a piano-inspired software voice; this is an offline preview timbre, not a bundled sampled grand-piano soundfont.
- A browser UI that displays the imported score via locally bundled OpenSheetMusicDisplay, presents voice/tempo controls, and offers MIDI/WAV downloads.

## Known limitations

- A score with unusual part naming may need manual SATB mapping.
- Repeats/navigation signs and all engraving details are not yet guaranteed to follow every notation convention.
- Matching the exact MuseScore layout, robust cursor-to-note synchronization for every score, and final MP4/MOV export need additional implementation and cross-score validation. The app should not be treated as production-complete until those validations pass.
