# DirectorLoop final goal (authoritative)

Received from Leon on 2026-09-12, about 13:50 PDT. This supersedes earlier product priorities where they conflict
(`docs/SPEC.md`, the UGC creative-research pivot prompts in `~/Downloads/DirectorLoop_*`, and
`/Users/leon/Documents/ChatGPT/HACKATON/DIRECTORLOOP_CLAUDE_IMPLEMENTATION_HANDOFF.txt`). Those remain history and
supply compatible technical requirements. The text below is kept as given.

---

The final end goal is:

"Upload a video. DirectorLoop reviews it as an unfamiliar audience member, identifies the exact moments where attention,
understanding, emotional response, or motivation to engage may weaken, explains why using evidence from the actual video,
proposes precise repairs, and checks whether the rendered revision actually improves those moments."

The cold-audience audit is the core product. Editing, evaluation, and learning must serve that experience.

## 1. Establish reality and preserve the existing work

Locate and inspect the actual repository (last reported: /Users/leon/Desktop/dev/CoreWeave). Inspect repository
instructions, git status, active work, CURRENT_STATE.md, services, tests, artifacts, and existing Weave integration.
Coordinate with any active implementation work before editing overlapping files. Inspect relevant existing Curio work at
/Users/leon/Desktop/dev/Curio-Automation. Read existing research under /Users/leon/Documents/ChatGPT/HACKATON
(DIRECTORLOOP_OPEN_SOURCE_STACK.md, research/ugc-creative-research/curio-reuse-findings.md,
research/ugc-creative-research/typesafe-hackathon-findings.md). Verify these paths and files.

Reuse the renderer, CreativeGenome, evaluation infrastructure, mutation engine, policy memory, review routes, jobs, and
React interface wherever they already work. Make the minimum architectural changes needed. Continue into implementation
after inspecting.

## 2. Use the Curio audit as the behavioral reference

Leon's supplied Curio log demonstrates the required specificity:
- Beavers, 8.59-9.97 s: the crate is open, but the animals have not exited. The review identifies unnecessary waiting and
  proposes starting the exit earlier while preserving the landing and opening mechanism.
- Rock, 6.93-10 s: the discovery and age have already been delivered, leaving an extended closing choice. The review
  identifies a weak reason to continue after the payoff.
- Office, 3.35-4.69 s: explanation continues without enough new visual action.
- Ocean: the scientific conclusion arrives before the discovery earns it. The review proposes restoring the sequence of
  spill, recovery, clues, and consequence.
- Across revisions: understanding sometimes improves while the opening, emotional payoff, or ending becomes weaker.

These are reference findings from a previous review. Reinspect the actual matching files before treating them as verified
observations. Do not hard-code these timestamps or conclusions into the application. Match the quality of reasoning:
where, what, why, suggested change, protected strengths, and honest comparison.

## 3. Inspect the actual rendered audiovisual experience

Consider actual frames and motion; speech and its timing; captions and other visible text; music, silence, sound effects,
and synchronization; narrative progression and visual evidence; phone-scale readability and subject visibility.

Perform an initial unprimed audience review without the creator's explanation, previous verdict, proposed repair, or any
label implying a version is improved. For moment-by-moment reactions, review chronological windows or prefixes so later
information cannot silently erase earlier confusion. Follow with a diagnostic pass that can inspect the whole story and
relevant source evidence. Keep factual verification against privileged source material separate from what an unfamiliar
viewer could understand from the video alone. Use broad temporal inspection followed by closer inspection around suspected
problems.

Record which media, timestamps, frames, and audio inputs were actually inspected. Distinguish native-video input, sampled
frames, transcript-based review, and unavailable audio. Never present sampled frames plus a script as verified
frame-by-frame audiovisual review. Partial coverage must remain visible.

## 4. Produce an actionable, evidence-backed audience audit

Each finding: finding ID and exact video-version ID; start/end time and representative frame or clip evidence; directly
observed visual, audio, or caption facts; the viewer's likely understanding or expectation at that moment; predicted viewer
reaction (labeled as model judgment); suspected weakness and alternative explanations; affected objective; severity and
uncertainty; specific proposed repair; what should remain unchanged; repair feasibility and required source material.

Separate observations from interpretations. Issue types include unclear opening or subject; missing context or ambiguous
references; narration/visual mismatch; unreadable or competing text; repetition without new information; delayed expected
action; prematurely resolved mystery; weak or missing payoff; story order that undermines discovery; framing that hides the
important action; weak emotional or social relevance. Do not force findings into every interval. Record strong sections
worth preserving. Stillness, silence, slow pacing, and delayed answers can be appropriate. Every timestamped finding must
open the relevant media.

## 5. Assess audience responses separately

Reasoned predictions (YES, MAYBE, NO, INSUFFICIENT_EVIDENCE with brief explanations) for: would the opening make an
unfamiliar viewer stop; would they continue and where might interest weaken; would they finish; a plausible reason to like;
would they send it, to whom and why; a natural reason to comment; value in saving or replaying; a reason to visit the creator
or product when relevant. Do not assume every video needs every action. Do not translate model judgments into fabricated
retention percentages or a fake drop-off curve; use "predicted attention risk".

## 6. Connect findings to repairs the system can actually perform

A. Existing-video edits (trim, reorder, shorten pauses, supported reframing, caption changes when separately editable).
B. Source-project edits (animation timing, camera movement, object motion, composition) requiring the original project;
   inspect Curio-Automation for reusable source-level controls and add bounded adapters where feasible.
C. New or replacement assets; report the missing dependency; generate only when available, permitted, and useful.

Do not imply a flattened MP4 allows independent movement of an animated animal or building, or clean replacement of
burned-in captions unless actually supported. Return a useful diagnosis when the repair cannot be automated. Use typed,
validated edit operations; preserve facts, identity, speech meaning, required content and protected intervals. Prefer the
smallest coherent repair and record unavoidable secondary changes. Track how original intervals map to the new render.

## 7. Review the rendered revision independently

Preserve the original; save the finding and repair hypothesis; render a real candidate; verify the intended change occurred;
review the candidate in a fresh context under the same frozen criteria; compare the targeted issue and the surrounding story.
The candidate reviewer must not receive the editor's desired verdict or the previous criticism as instructions to agree.
Report what improved, regressed, remained unchanged, whether the original weakness was resolved, whether a new weakness
appeared, and the overall outcome (improvement, regression, mixed result, tie, insufficient evidence). Do not average away
tradeoffs. Use representative repeat or reversed-order comparisons to inspect reviewer instability. Keep states distinct:
proposed, rendered, reviewed, accepted, rejected, incomplete. A successful process exit is insufficient when structured QA
reports INCOMPLETE.

## 8. Make the interface show the actual product

Large video player; timeline with clickable findings and preserved strengths; evidence frames or short replayable
intervals; cold-audience verdict panel; repair cards with feasibility and protected content; a clear action to run supported
repairs; before/after playback with interval alignment; comparison of gains, regressions and remaining weaknesses; review
coverage and evidence provenance. Metadata stays inspectable without raw JSON as the main experience.

## 9. Preserve learning and sponsor integration as supporting capabilities

Save video characteristics -> finding -> repair -> actual result -> uncertainty -> conditions where the lesson may apply.
Retain failures and neutral outcomes; separate model predictions, human evidence, reference priors, and real platform
outcomes. Cross-video policy transfer remains useful after the core audit-and-repair experience works; it must not replace it
or require a manufactured Video B success. Keep Weave central (media analysis, cold review, evidence inspection, diagnosis,
repair selection, rendering, fresh review, comparison, memory update). Use TypeSafe only within verified capabilities; a
judgment over extracted evidence is not direct video viewing. Maintain an inventory distinguishing pre-existing Curio
components from hackathon work.

## 10. Verify the product against real footage

Demonstrate: a real owned video loads and plays; the audit uses its actual rendered media; coverage and limitations are
recorded; findings link to inspected frames or intervals; audience predictions have evidence and uncertainty; strong sections
are identified; repair suggestions identify required capabilities; at least one supported repair produces a real playable
revision; a fresh review evaluates it; the comparison checks the target issue and regressions; history and Weave trace are
inspectable; a second real video receives an audit without hard-coded findings. Test failure cases: incorrect timestamps,
missing audio, stale renders, transcript-only reviews, unsupported repairs, mislabeled versions, lost protected content,
falsely successful incomplete QA. Do not use "the score increased" as the sole acceptance criterion.

## 11. Demonstration

original clip -> cold-audience finding -> exact evidence -> targeted repair -> revised clip -> fresh verdict -> recorded
lesson. Show saved runs as saved runs; run live only when measured timing supports it; include real human results only if
available and separate from model judgments.

Pitch: "DirectorLoop shows where your video may lose an unfamiliar viewer, explains why, and tests whether a specific edit
actually fixes it."

## 12. Checkpoint

Save this direction as DIRECTORLOOP_FINAL_GOAL.md and reference it from CURRENT_STATE.md. Report what works, what is
partial or blocked, artifact paths, representative findings, repairs and outcomes, tests, timings and costs, Weave URLs,
start commands and the demo entry point.
