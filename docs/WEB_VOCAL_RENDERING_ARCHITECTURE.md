# Superseded: Web Vocal Rendering Architecture

This document records an earlier hosted-GPU proposal. It is **not the selected architecture** for the current MIDI-MP4 implementation.

The selected design is laptop-first and local-only: see [LOCAL_RENDERING_ARCHITECTURE.md](LOCAL_RENDERING_ARCHITECTURE.md). The local machine runs the API, media pipeline and (after the headless renderer milestone) the vocal inference engine. No Cloudflare Worker, queue, R2 bucket or external GPU provider is required for that workflow.

The proposal below is retained as historical design context only. Do not implement it unless the project owner explicitly changes the deployment decision.

