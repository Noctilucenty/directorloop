# Pack A: cereal-box phone stand (rendered fixture), fixture v3

Status: fictional graphical demonstration, procedurally rendered by `scripts/make_pack_a.py`.
There is no real product and no recorded footage in this pack. The spec (section 6) allows a
clearly labeled fictional graphical demo when recorded footage is unavailable; this is that case.

## Provenance and rights

- Every video asset is drawn with Pillow from a deterministic scene description and encoded with
  ffmpeg (libx264, single-threaded, bitexact muxing). No third-party images, fonts, or clips.
- Music is a synthesized pad (numpy), 12 s, peak -20 dBFS. No licensed audio.
- Narration: provider `elevenlabs`, model/voice `eleven_v3`, 6720 ms. Reused from the previous generation of this pack (hash unchanged, no new synthesis).
  Script (exact): "I made this phone stand out of a cereal box. Set it up, lock it, and it holds. Even when you tap around on the screen."
  The voiceover deliberately does not explain the mechanism verbally, which is how first drafts
  commonly go; the visuals have to carry that information.
- Rendered at 1080x1920, 30 fps. Durations: {"wide": 6000, "closeup": 3000, "result": 4000, "narration": 6720, "music": 12000} (ms).

## What each shot establishes

- wide shot: the whole desk from a desk-level phone-camera angle, with a pencil cup standing on the
  desk in front of the stand. The cup hides the base of the stand, so the orange tab, its guide
  loops and the slot are never visible in this shot (0 tab-coloured pixels in every frame). A hand
  reaches in from the camera side and presses behind the cup (2.0-3.7 s), withdraws, and a phone is
  placed on the stand (3.8-5.0 s). A viewer of this shot sees a hand reach behind the cup and a
  phone being placed. How the stand locks is genuinely not visible. This is the realistic case the
  loop is meant to catch, not a sabotaged edit: the lock was filmed, but desk clutter hides it.
- close-up: the tab, its two guide loops and the slot fill the frame; a fingertip arrives from the
  upper right and touches only the top edge of the tab, so the tab body, loops and slot stay
  visible beside it while the tab is pressed down and seats into the slot (0.3-2.2 s).
- result shot: the phone on the locked stand; a fingertip taps the screen twice at 1.5 s and 2.1 s
  and nothing moves. The phone body covers the seated tab, so this shot shows no tab either.
- The baseline edit uses only the wide and result shots. The close-up exists in the asset pool but
  is not used, which is the ordinary first-cut situation the loop is meant to catch.

## Fixture history

- v1: the wide shot showed the fingertip beside the tab; a frame-based viewer (8 frames at 512 px)
  could still read "a finger pushes the small orange tab down into the base", so the wide-shot
  baseline passed the mechanism questions. The no-media control was clean, so the questions did
  not leak; the fixture was simply too legible.
- v2: the hand occluded the tab only during the press. The same viewer read the before and after
  states instead: the orange tab was visible above the base before the press (about 14 px tall at
  512 px) and a sliver remained after it, and it answered "a finger pushes the small orange tab
  downward into the base". Before and after states also communicate a mechanism. Suite options were
  rebalanced in v2 so every option describes a plausible cardboard mechanism.
- v3 (current): a pencil cup in front of the stand hides the whole mechanism in the wide shot, and
  the mechanism moved 6 world units left so the phone covers the seated tab in the result shot.
  The close-up is unchanged apart from recentering on the moved tab. Declared claims in the manifest
  still describe what the creator believes each shot shows (the creator believes the wide shot shows
  the lock); pixel coverage analysis in the loop is what has to catch the mismatch.

## Replacing the fixture with real footage

Record with a phone in portrait, steady, good light, no music playing:

1. wide: the whole desk with the stand small in frame; perform the lock, then place the phone.
2. close-up: fill the frame with the locking mechanism; perform the lock slowly.
3. result: three-quarter view of the phone on the locked stand; tap the screen twice.
4. Record the voiceover separately (or reuse this script) as a mono WAV.

Then copy the files into `assets/` named by their sha256 (`shasum -a 256 file`), update
`assets/manifest.json` (origin `recorded`, rights note, declared claims), keep `brief.json`,
`source_truth.json` and `suite.json` if the mechanism is the same, and run
`.venv/bin/python -c "from directorloop.domain import write_checksums; write_checksums('packs/pack_a_stand_demo')"`.

## Integrity

`checksums.json` lists the sha256 of every JSON file and media asset. `load_pack` verifies them.
Narration from ElevenLabs is not bit-reproducible between generations, so regenerating the pack
changes the narration hash and rewrites `checksums.json`; the video and music hashes are stable.
