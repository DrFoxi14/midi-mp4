# MIDI-MP4 — local SATB workstation

Local-first workstation for importing SATB scores, inspecting notation, generating separate MIDI tracks, previewing parts with a local synthesizer, and rendering synchronized score videos.

**Status: active beta, not production-certified.** MIDI generation, the local browser interface, a synthetic piano-like preview, and a first video-rendering pipeline are implemented. Broad validation on real SATB scores and faithful preservation of MuseScore's original engraving are still outstanding.

## Privacy model

- Score files and generated media stay on the machine running the application.
- Normal score-processing and video-rendering jobs do not call cloud APIs or upload scores to an external server.
- The web server binds to `127.0.0.1` by default. Do not expose this development server to a public interface.
- Initial installation downloads open-source packages. Chromium must also be installed for local geometry calculation.

## Requirements

- Python 3.11+
- Node.js/npm (local score display renderer)
- FFmpeg with `libx264` and AAC encoder support (video export)
- Playwright's local Chromium browser (video export)
- MuseScore 4 CLI (optional; necessary for direct `.mscz` import)

## Setup on macOS

From the cloned repository directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
npm install
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765. Check http://127.0.0.1:8765/api/health for installed video dependencies. If FFmpeg is not installed, install it using the package manager you already use on the Mac.

MuseScore 4 CLI is detected on PATH or in its standard macOS application location. If it is unavailable, export the score from MuseScore as MusicXML and import that file.

## Tests

```bash
python -m pytest
```

The suite checks tempo scaling, tempo changes, track separation, tied-note merging, SATB mapping, a minimal four-part MusicXML fixture, and video timeline helper behavior. It has not yet been validated against a broad library of real choral scores.

## Implemented

- Import MusicXML `.musicxml`, `.xml`, compressed MusicXML `.mxl`, and MuseScore `.mscz` using the local MuseScore CLI for conversion.
- Parse notes, rests, durations, pitches, ties, measures and tempo-mark positions with music21.
- Record original part order and apply SATB-name detection with an order-based fallback for common four-part exports.
- Generate multitrack MIDI with independent Soprano, Alto, Tenor and Bass tracks; download the full file or individual voice files.
- Change reference tempo while preserving tempo ratios. Example: original markings 60 and 90 BPM become 80 and 120 BPM at reference 80.
- Generate an offline piano-inspired WAV preview with selectable parts and a global offset.
- Render MusicXML locally with OpenSheetMusicDisplay in the browser.
- Render MP4/MOV using Verovio SVG engraving, note-event highlights, a moving vertical cursor, the adjacent-system viewport, a synthesized WAV track, and local FFmpeg H.264/AAC encoding.
- Select a full-score video or an individual voice, resolution (1080p/4K), container (MP4/MOV), and isolated/accompaniment preview mode.
- Run video renders as local background jobs with progress and a download endpoint.

## Known limitations

- **Video engraving is produced by Verovio.** It may look different from the original MuseScore page layout; exact layout preservation has not been claimed.
- The current preview is an offline synthesized timbre, not a bundled sampled grand piano or a human-vocal synthesizer. In accompaniment mode, the selected part is emphasized in the synthetic mix.
- Individual video export assumes one notated staff per SATB part, ordered as in the source. Unusual grand-staff/condensed layouts may require further mapping.
- Very long scores that cannot fit Verovio's single continuous canvas are rejected rather than silently truncated. The current hard duration cap is 20 minutes per export.
- Repeats/navigation signs, transposing-instrument notation, unusual tempo placement, and every MusicXML edge case are not guaranteed to be handled identically to a conductor's performance.
- The cursor's note mapping and rendering have not yet been validated across a broad collection of real SATB arrangements. A tiny synthetic fixture is not sufficient evidence of production readiness.

The application labels unavailable dependencies and export errors explicitly. Check `GET /api/health` for local dependency status.
