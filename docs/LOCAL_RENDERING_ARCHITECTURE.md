# Local Rendering Architecture

Status: selected architecture for the Mac/laptop workflow. This supersedes the hosted-GPU proposal in `WEB_VOCAL_RENDERING_ARCHITECTURE.md`.

## Decision

MIDI-MP4 remains a local application. The laptop runs the FastAPI backend, score parsing, audio processing, video rendering and (once the integration is completed) the vocal inference engine. The browser is the local user interface, not a remote service.

No SQL database is required for the current product. The application stores score source files, normalized MusicXML, imported vocal stems and exports under `MIDI_MP4_DATA_DIR` (default: `~/.local/share/midi-mp4`). Small JSON files store score metadata and annotations. Runtime job state is held in process memory. Temporary frame/audio files are created under per-job working folders and cleaned up after export.

## Rendering stack

- Music notation: OpenSheetMusicDisplay for interactive browser preview; Verovio for deterministic export engraving and note timing.
- Geometry: local Chromium/Playwright reads transformed SVG geometry. This is a measurement step, not the video renderer.
- Frame composition: CairoSVG rasterizes the notation background; Pillow draws the cursor and highlights.
- Encoding: FFmpeg receives raw RGB frames through stdin and produces H.264/AAC. The renderer does not retain a PNG for every frame.
- Singing: OpenUtau/DiffSinger and compatible locally installed singer/vocoder assets. The existing implementation currently generates a USTX project and imports manually rendered WAV stems; unattended local neural WAV rendering remains a separate engineering milestone.

## Three.js decision

Do not replace OSMD or Verovio with Three.js for ordinary sheet-music notation. OSMD/Verovio understand MusicXML engraving and expose score-note timing; Three.js is a general 3D/WebGL renderer and would require us to rebuild notation layout, hit-testing and timing-to-glyph mapping.

Three.js may be added later for optional 3D backgrounds, camera effects or visualizers. It is not a database optimization and does not reduce the amount of score/audio data that must be stored. For the current 2D sheet-music video, SVG + Pillow + FFmpeg is simpler, sharper and easier to verify.

## Memory and storage rules

1. Never keep every rendered frame in a Python list or in RAM.
2. Render/cache only the current musical-state background, compose one frame at a time, write raw RGB to FFmpeg, then release the frame.
3. Keep temporary work in a job-specific directory; remove it on both success and failure.
4. Keep large score/audio/video files on disk. Persist only small metadata JSON; do not add a database until a concrete query/concurrency requirement justifies one.
5. Prefer file-backed WAV intermediates for long recordings rather than making unnecessary full-size byte copies.
6. Limit concurrent 4K renders based on available RAM and CPU/GPU resources; expose progress and cancellation.

At 3840x2160, one RGB24 frame is about 23.7 MiB before intermediate copies. This is why frame-at-a-time streaming matters more than switching to Three.js.

## Lyric-to-note alignment contract

The source of truth is the MusicXML event timeline:

- Convert quarter-note positions and durations to OpenUtau ticks using 480 ticks per quarter.
- Place a lyric syllable on the exact note onset that carries its MusicXML lyric.
- Preserve each note's exact position, pitch and duration.
- For a lyricless note that starts exactly at the previous sung note's end, use OpenUtau's `+~` extender so DiffSinger can continue the vowel through a melisma.
- A rest or positive timing gap breaks the extension and becomes a pause; never carry a vowel across silence.
- Preserve syllable text and the Romanian phoneme hint. Keep the original MusicXML syllabic marker in the diagnostic timeline.
- Reject ambiguous overlaps/divisi instead of guessing a single vocal melody.
- Include a machine-readable alignment report with start/end ticks, duration, syllable, phonemes and event type for every note.

A whole note in common time is four quarter notes, so it must remain 1920 USTX ticks at 480 ticks per quarter. The DiffSinger duration model allocates that note span across the phonemes. It should keep consonants articulated and sustain vowel/diphthong sound as appropriate; it must not simply stretch the last written character.

## Acceptance tests

- Unit tests verify quarter-note-to-tick conversion, lyric onset assignment, full-note durations, melisma extension, rest/gap termination, tempo changes, and SATB lyric copying.
- Integration tests load a generated USTX in OpenUtau and render a licensed test bank to WAV.
- Audio checks compare rendered onset/end times with the score timeline and catch clipped consonants, short vowels, timing drift and silence leaks.
- Video tests verify output dimensions, frame rate, duration, audio stream, note-highlight events and cursor positions against the Verovio time map.
- A clean test render must be inspected at 1080p and 4K before a release is called production-ready.

“Perfect” is not a test result. Releases are accepted only against repeatable correctness criteria and real rendered fixtures. Neural voice quality still requires listening to the rendered audio with the actual licensed voicebank and vocoder.
