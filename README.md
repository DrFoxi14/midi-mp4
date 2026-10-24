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
- FFmpeg with `libx264` and AAC encoder support (video export); `libmp3lame` for MP3 exports
- Playwright's local Chromium browser (video export)
- MuseScore 4 CLI (optional; necessary for direct `.mscz` import)

## Quick launch on macOS

After cloning the repository, double-click `run-macos.command` in Finder or run `./run-macos.command` in Terminal. The launcher creates a Python virtual environment, installs project dependencies when needed, installs the local browser renderer and Playwright Chromium, starts the local server, waits for its health endpoint, and then opens the interface at `http://127.0.0.1:8765`. Keep the Terminal window open while using the app.

The launcher does not install Homebrew, FFmpeg, or MuseScore automatically. Without FFmpeg the video export remains unavailable, while import/MIDI/preview can still work. A first-time setup needs an internet connection to download open-source packages and Chromium; score processing and media generation remain on the local machine.

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

Open http://127.0.0.1:8765. Check http://127.0.0.1:8765/api/health for installed video dependencies, including the actual local Chromium binary and FFmpeg H.264/AAC encoders. If FFmpeg is not installed, install it using the package manager you already use on the Mac.

MuseScore 4 CLI is detected on PATH or in its standard macOS application location. If it is unavailable, export the score from MuseScore as MusicXML and import that file.

## Optional OMR-SATB import (PDF, scan or photo)

To import a scanned score or a phone photo directly, configure the separate local OMR-SATB repository. The app does not bundle its model weights:

```bash
export OMR_SATB_DIR="/absolute/path/to/omr-satb/omr"
# Optional: Python environment where OMR-SATB's dependencies are installed
export OMR_SATB_PYTHON="/absolute/path/to/omr-venv/bin/python"
```

The OMR folder must contain `piesa.py`, `step21_musicxml.py`, `pixnet.pt`, `symbols.pkl`, and `music_prior.pkl`. Use the Python environment with OMR-SATB's own requirements and assets. The health endpoint reports whether the app can locate the local model assets. PDF/photo import calls this local pipeline, then opens its MusicXML output in MIDI-MP4. The conversion can take several minutes per page; incomplete OMR runs are rejected rather than silently treated as complete scores.

## Optional sampled-piano sound

The default preview is synthesized and needs no extra assets. For a sampled piano, install FluidSynth locally and point the app at a SoundFont file you already have:

```bash
export MIDI_MP4_SOUNDFONT="/absolute/path/to/your-piano.sf2"
# Optional if FluidSynth is not named "fluidsynth" or not on PATH:
export MIDI_MP4_FLUIDSYNTH="/absolute/path/to/fluidsynth"
```

The app accepts local `.sf2` and `.sf3` files through the "Alege SoundFont local" control (max 512 MB), or via `MIDI_MP4_SOUNDFONT`. The uploaded file is posted only to the local API at 127.0.0.1 and saved under the local application data folder; no cloud service is involved. If FluidSynth is missing, the interface says so and keeps the built-in synthesized preview available.

## Tests

```bash
python -m pytest
```

The suite checks tempo scaling, tempo changes, MIDI track separation, tied-note merging, SATB mapping, per-voice mixer levels and validation, a minimal four-part MusicXML fixture, video timeline helpers, and API-level consent/voice validation. When the full local video toolchain is available, it also renders the tiny SATB fixture to an actual MP4 and verifies the output. This is a build smoke test, not proof of correctness for a broad library of real choral scores.

## Implemented

- Import MusicXML `.musicxml`, `.xml`, compressed MusicXML `.mxl`, and MuseScore `.mscz` using the local MuseScore CLI for conversion. When OMR-SATB is configured locally, also import PDF scans and image files through its PDF/photo-to-MusicXML pipeline.
- Parse notes, rests, durations, pitches, ties, measures and tempo-mark positions with music21.
- Record original part order and apply SATB-name detection with an order-based fallback for common four-part exports.
- Generate multitrack MIDI with independent Soprano, Alto, Tenor and Bass tracks; download the full file or individual voice files.
- Change reference tempo while preserving tempo ratios. Example: original markings 60 and 90 BPM become 80 and 120 BPM at reference 80.
- Generate an offline piano-inspired WAV preview with selectable parts and a global offset. An optional sampled-piano backend uses local FluidSynth plus an existing local SF2/SF3 SoundFont; without that configuration it explicitly falls back to synthesized audio.
- Export a full SATB mix, an isolated part, or a foreground voice with accompaniment as local WAV or MP3; export a standalone ZIP with a full mix and separate tracks.
- Package MusicXML and full/per-voice MIDI for OpenUtau/DiffSinger; import rendered PCM vocal WAV stems back into MIDI-MP4 so preview, audio/video exports and rehearsal packs use real/AI-sung parts while missing parts use the local synth fallback.
- Adjust each choir part's playback level independently (0–100%) in the conductor mixer; the same mix is applied to generated WAV previews and exported video audio.
- Run non-destructive preflight checks for practical vocal range, large melodic leaps, voice crossings, and missing parts. If the score came through OMR-SATB, surface its low-confidence notes and Music Intelligence suspicions, with a link to the full analysis report.
- Analyze a recorded, isolated singer from a local PCM WAV and compare estimated fundamental pitch against the selected score part. The report marks notes in tolerance, sharp/flat deviations, unclear samples, and overlapping-note events it skipped.
- Define measure-range rehearsal loops, use a 1- or 2-bar count-in, and optionally raise/lower reference tempo in steps on each repetition.
- Save conductor notes to measures, tagged by voice and rehearsal topic. Notes are persisted locally beside the score.
- Create a rehearsal-pack ZIP with MusicXML, full-score and per-voice MIDI/WAV, offline HTML pages for individual singers, conductor annotations, and the preflight report. An optional checkbox renders an MP4 for the full score and each voice.
- Maintain a concert setlist with ordering controls and current/next piece display. Browse the saved-score library from the interface, reopen a persisted score after app/browser restarts, and delete an old or damaged local record safely. The setlist itself stays in browser local storage.
- Render MusicXML locally with OpenSheetMusicDisplay in the browser.
- Render MP4/MOV using Verovio SVG engraving, note-event highlights, a moving vertical cursor, the adjacent-system viewport, a synthesized WAV track, and local FFmpeg H.264/AAC encoding.
- Select a full-score video or an individual voice, resolution (1080p/4K), container (MP4/MOV), and isolated/accompaniment preview mode.
- Run video renders as local background jobs with progress and a download endpoint.

## Known limitations

- **Video engraving is produced by Verovio.** It may look different from the original MuseScore page layout; exact layout preservation has not been claimed.
- The current preview is an offline synthesized timbre, not a bundled sampled grand piano or a human-vocal synthesizer. Neural singing uses the explicit OpenUtau/DiffSinger handoff: no vocal model/voicebank is bundled and neural rendering happens externally before WAV stems are re-imported. MP3 export requires local FFmpeg with `libmp3lame`; WAV does not. In accompaniment mode the selected part is emphasized.
- PDF/photo recognition is optional and depends on a correctly configured OMR-SATB installation. Its uncertainty report is advisory, not proof that every note was read correctly.
- Preflight warnings are heuristics and do not edit the score. Practical range values vary by singer and voice type; crossings and leaps may be intentional. Recording analysis is limited to one isolated singer/part at a time; it is not live polyphonic choir transcription or a substitute for listening. Timing alignment uses the chosen tempo and a global offset rather than automatic performance alignment.
- Individual video export assumes one notated staff per SATB part, ordered as in the source. Unusual grand-staff/condensed layouts may require further mapping.
- Very long scores that cannot fit Verovio's single continuous canvas are rejected rather than silently truncated. The current hard duration cap is 20 minutes per export. Each concurrent video job has its own temporary frame/audio directory to avoid cross-job collisions.
- Repeats/navigation signs, transposing-instrument notation, unusual tempo placement, and every MusicXML edge case are not guaranteed to be handled identically to a conductor's performance.
- The cursor's note mapping and rendering have not yet been validated across a broad collection of real SATB arrangements. A tiny synthetic fixture is not sufficient evidence of production readiness.

The application labels unavailable dependencies and export errors explicitly. Check `GET /api/health` for local dependency status.
