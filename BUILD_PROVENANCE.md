# Build provenance

New work: everything in this repository was written during CoreWeave Hacks starting September 12, 2026
(see git history). No code was copied from the author's earlier Curio automation repositories; the
problem framing comes from that experience and is described in `docs/SPEC.md`.

Pre-existing tools on the build machine (not part of the submission):
- ffmpeg 8.1.2 (Homebrew build: libx264, videotoolbox; no libfreetype/libass, so captions are Pillow PNG overlays)
- whisper.cpp `whisper-cli` with `ggml-large-v3-turbo-q5_0` for transcription of rendered audio
- macOS `say` as a zero-cost narration fallback; ElevenLabs when a key is configured
- Python 3.12 via uv; Node 24 for the web client

Third-party services and licenses are listed in `docs/VERIFIED_CAPABILITIES.md` once smoke-tested.
Demo media provenance is recorded per pack in `packs/<pack>/PACK.md`.
