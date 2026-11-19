# Web Vocal Rendering Architecture

Status: architecture decision; implementation is staged. This document does not claim that cloud singing render is already operational.

## Decision

Keep MIDI-MP4's existing local-first workflow working. Add an optional hosted rendering path behind a provider-neutral API:

- Frontend: existing MIDI-MP4 web UI.
- Edge/API: Cloudflare Worker for authentication, validation, job creation and status.
- Queue: Cloudflare Queues to decouple HTTP requests from long GPU jobs.
- Object storage: Cloudflare R2 for short-lived input/output artifacts, using scoped, expiring upload/download URLs.
- Renderer: a private container service on a GPU-capable host. It owns model execution and never exposes model files publicly.
- Video: existing MIDI-MP4 video pipeline consumes the four rendered WAV stems and produces the synchronized MP4.

GitHub stores source and CI/CD configuration. GitHub Pages or a static frontend cannot perform neural inference; GitHub Actions should deploy code, not act as the runtime GPU.

## Job flow

1. Browser uploads MusicXML and user-selected singer IDs to the MIDI-MP4 API.
2. API validates file size/type, lyric presence, voicebank compatibility and user/job limits.
3. API creates a job record and queues a render request.
4. Worker/queue consumer requests the job from the private renderer.
5. Renderer creates/validates the SATB USTX project and renders S/A/T/B WAV stems.
6. Renderer writes results to temporary object storage and reports completion.
7. Browser polls job status (or later uses SSE/WebSocket) and downloads the result.
8. MIDI-MP4 imports the stems into its current mixer/video pipeline and renders the MP4.

## Renderer strategy

Preferred first implementation: extract a deterministic headless render service from OpenUtau.Core and expose it through a small .NET CLI/container API. Reuse OpenUtau's existing DiffSinger renderer, phonemizers, singer discovery, ONNX acoustic inference and vocoder integration. Do not implement a second DiffSinger engine unless the OpenUtau extraction proves infeasible.

The current OpenUtau-CLI headless notes describe substantial blockers: UI-thread scheduling, asynchronous phonemizer completion, singleton initialization, internal render APIs and plugin/singer discovery. Treat headless rendering as an engineering milestone, not as a solved capability. Start with a CLI that loads a known USTX fixture and renders one known licensed test bank; then add SATB parallelism and service endpoints.

## API contract (v1)

- `POST /v1/jobs`: create a render job; returns `job_id` and status URL.
- `GET /v1/jobs/{job_id}`: queued/running/succeeded/failed status and progress.
- `GET /v1/jobs/{job_id}/result`: short-lived download links for four WAV stems and a render report.
- `DELETE /v1/jobs/{job_id}`: request cancellation and artifact cleanup.

The browser must never receive GPU-host credentials, storage master keys or model download credentials. Use short-lived scoped URLs, authenticated API requests, strict CORS, request limits, job quotas, timeouts, and automatic deletion of temporary score/audio artifacts.

## Model and voicebank licensing

Do not commit model weights, vocoders or singer banks to this repository or bake them into public container images by default. Store approved assets privately and load them at runtime. Before enabling a singer bank in a public service, verify the exact model, vocoder, dataset and voicebank terms, including commercial use, redistribution, hosting and generated-audio restrictions. A Romanian phoneme inventory alone does not establish permission to host its voice model.

## Deployment boundaries

- Cloudflare Worker: API/control plane only.
- Cloudflare Queues: durable asynchronous job dispatch.
- Cloudflare R2: temporary artifacts only.
- GPU container: actual neural inference.
- GitHub: source, tests and deployment automation.

A production deployment requires the owner to authorize a GPU provider and configure secrets in the provider/GitHub secret store. Never paste API tokens into chat or commit them.

## Acceptance gates

1. Existing local workflow and tests remain green.
2. Headless OpenUtau CLI renders a small known USTX fixture to a valid WAV in a clean container.
3. Romanian native phonemes work with a specifically licensed Romanian-capable singer/vocoder pair.
4. Four SATB tracks render independently and preserve score timing.
5. API handles queued, failed, cancelled and completed jobs without leaking artifacts.
6. Browser can submit a score, watch progress, preview stems and export a synchronized MP4.
7. Load, cost, privacy, retention and licensing policies are documented before public launch.

## Non-goals for the first cloud milestone

- No public hosting of unlicensed voicebanks.
- No GPU inference inside a Cloudflare Worker.
- No silent fallback to a different language or singer when a requested phoneme is unsupported.
- No removal of the existing local/manual WAV import workflow.
