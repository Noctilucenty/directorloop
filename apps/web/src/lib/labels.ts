// Plain-language names for identifiers the API returns. Unknown identifiers fall back to a readable form.

const MUTATIONS: Record<string, string> = {
  HOOK_REWRITE: "Rewrite the hook",
  HOOK_VISUAL_SWAP: "Swap the opening visual",
  RESULT_FIRST: "Open with the result",
  PROOF_EARLIER: "Move the proof earlier",
  PAYOFF_EARLIER: "Move the payoff earlier",
  CONTEXT_COMPRESSION: "Remove one context beat",
  REMOVE_REDUNDANT_BEAT: "Remove a repeated beat",
  SHOT_SWAP: "Swap a shot",
  SHOT_REORDER: "Reorder shots",
  SHORTEN_SHOT: "Trim dead air",
  EXTEND_PROOF: "Hold the proof longer",
  CAPTION_COMPRESSION: "Shorten captions",
  CAPTION_EMPHASIS: "Emphasize a caption",
  PATTERN_INTERRUPT: "Punch in during a static stretch",
};

const FAMILIES: Record<string, string> = {
  proof_latency: "The answer arrives late",
  context_interruption: "Context stalls the momentum",
  visual_stagnation: "The picture stops changing",
  hook_mismatch: "The hook promises something else",
  weak_hook_tension: "The opening raises no question",
  open_loop_too_long: "The opening question is held too long",
  redundant_beat: "A beat repeats itself",
  dead_air: "Dead air",
};

const COMPONENTS: Record<string, string> = {
  hook_topic_comprehension: "Opening names the topic",
  message_comprehension: "Core message understood",
  model_full_preference_vs_control: "Continue-watching preference, full video",
  model_hook_preference_vs_control: "Continue-watching preference, first 3 s",
  payoff_ms: "Payoff starts at",
  context_before_payoff_ms: "Context before payoff",
  first_visual_change_ms: "First visual change",
  longest_static_span_ms: "Longest static stretch",
  duration_ms: "Duration",
  render_ms: "Render time",
};

const DECISIONS: Record<string, string> = {
  winner: "Winner found",
  no_clear_winner: "No clear winner",
  all_rejected: "All variants rejected",
  insufficient_evidence: "Insufficient evidence",
};

const OBJECTIVES: Record<string, string> = {
  attention: "Attention",
  comprehension: "Comprehension",
  "emotional payoff": "Emotional payoff",
  emotional_payoff: "Emotional payoff",
  sharing: "Sharing",
  motivation: "Motivation",
};

export const AUDIENCE_LABELS: Record<string, { name: string; question: string }> = {
  stop: { name: "Stop", question: "Would the opening stop an unfamiliar viewer?" },
  continue: { name: "Continue", question: "Do they keep watching, and where could interest weaken?" },
  finish: { name: "Finish", question: "Do they reach the end?" },
  like: { name: "Like", question: "Is there a moment worth a like?" },
  send: { name: "Send", question: "Would they send it, to whom and why?" },
  comment: { name: "Comment", question: "Is there something to say back?" },
  save: { name: "Save / replay", question: "Is it worth keeping or watching again?" },
  visit: { name: "Visit", question: "Would they go to the account?" },
};

function readable(id: string): string {
  const s = id.replace(/_/g, " ").toLowerCase();
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

export const mutationName = (id: string | null | undefined) => (id ? MUTATIONS[id] ?? readable(id) : "Original");
export const familyName = (id: string) => FAMILIES[id] ?? readable(id);
export const componentName = (id: string) => COMPONENTS[id] ?? readable(id);
export const decisionName = (id: string | null | undefined) => (id ? DECISIONS[id] ?? readable(id) : "Pending");
export const objectiveName = (id: string) => OBJECTIVES[id] ?? readable(id);
export const statusName = (id: string) => id.replace(/_/g, " ");

export function armName(label: string): string {
  return label === "control" ? "Control" : `Arm ${label}`;
}

export type RoleGroup = "hook" | "build" | "evidence" | "payoff" | "other";

/** Beat roles share four color groups (validated palette); every beat also carries its role label. */
export function roleGroup(role: string): RoleGroup {
  switch (role) {
    case "hook":
      return "hook";
    case "setup":
    case "context":
    case "problem":
    case "tension":
      return "build";
    case "proof":
    case "mechanism":
      return "evidence";
    case "payoff":
    case "cta":
      return "payoff";
    default:
      return "other";
  }
}

export const SOURCE_NAMES: Record<string, string> = {
  mechanical: "mechanical measurement",
  asr: "speech recognition",
  vision_model: "vision-model label",
  language_model: "language-model label",
  creator_declared: "declared by the creator",
  derived_from_plan: "derived from the edit plan",
};

// ---------------------------------------------------------------------------------------------
// Creator flow vocabulary: every internal identifier the run and audit APIs return, in plain words.
// ---------------------------------------------------------------------------------------------

const ISSUE_TYPES: Record<string, string> = {
  unclear_opening_or_subject: "Unclear opening or subject",
  missing_context_or_ambiguous_reference: "Missing context",
  narration_visual_mismatch: "Narration and picture disagree",
  unreadable_or_competing_text: "Text is hard to read",
  repetition_without_new_information: "Repeats without new information",
  delayed_expected_action: "The expected action comes late",
  prematurely_resolved_mystery: "The mystery is resolved too early",
  weak_or_missing_payoff: "Weak or missing payoff",
  story_order_undermines_discovery: "Story order spoils the discovery",
  framing_hides_important_action: "Framing hides the important action",
  weak_emotional_or_social_relevance: "Weak emotional or social pull",
  visual_stagnation: "The picture stops changing",
  other: "Other",
};

export const issueName = (id: string) => ISSUE_TYPES[id] ?? readable(id);

const FOCUS: Record<string, string> = {
  attention: "Attention",
  comprehension: "Understanding",
  emotional_payoff: "Emotional payoff",
  sharing: "Sharing",
  motivation_to_engage: "Motivation to engage",
  other: "Other",
};

export const focusName = (id: string | null | undefined) => (id ? FOCUS[id] ?? readable(id) : "");

export const AUDIENCE_NAMES: Record<string, string> = {
  stop: "Stop scrolling",
  continue_watching: "Keep watching",
  finish: "Finish",
  like: "Like",
  send: "Send to someone",
  comment: "Comment",
  save_or_replay: "Save or replay",
  visit_creator: "Visit the creator",
};

export const ROUTE_NAMES: Record<string, string> = {
  A: "Can be edited in this file",
  B: "Needs the original project",
  C: "Needs new footage or audio",
};

export function decisionHeadline(decision: string): { label: string; tone: "good" | "bad" | "warn" | "neutral" } {
  switch (decision) {
    case "accept":
      return { label: "Kept", tone: "good" };
    case "reject_keep_current":
      return { label: "Not kept", tone: "bad" };
    case "incomplete_keep_current":
      return { label: "Could not be judged", tone: "warn" };
    case "stop":
      return { label: "Stopped", tone: "neutral" };
    default:
      return { label: readable(decision), tone: "neutral" };
  }
}

export const OUTCOME_NAMES: Record<string, string> = {
  improvement: "Improvement",
  regression: "Worse",
  mixed: "Mixed",
  tie: "No reliable difference",
  insufficient_evidence: "Not enough evidence",
};

const JUDGE_STAGE: Record<string, string> = {
  STARTED: "Started",
  AUDITING: "Watching as a cold viewer",
  ANALYZING: "Reading picture and sound",
  COLD_REVIEW: "Cold review",
  DIAGNOSING: "Finding weak moments",
  VERIFYING: "Checking close-ups",
  AUDIT_DONE: "Review complete",
};

const ATTEMPT_STAGE: Record<string, string> = {
  SELECTING: "Choosing an edit",
  REPAIRING: "Rendering the edit",
  VERIFYING: "Checking the render",
  FRESH_REVIEW: "Fresh review of the revision",
  ANALYZING: "Reading the revision",
  COLD_REVIEW: "Cold review of the revision",
  DIAGNOSING: "Finding weak moments in the revision",
  AUDIT_DONE: "Fresh review complete",
  COMPARING: "Comparing original and revision",
  DECIDED: "Decision",
};

export function stageName(stage: string, phase: "judge" | "attempt" | "end"): string {
  if (stage === "DONE") return "Done";
  if (stage === "FAILED") return "Failed";
  if (stage === "CANCELED") return "Canceled";
  const table = phase === "attempt" ? ATTEMPT_STAGE : JUDGE_STAGE;
  return table[stage] ?? readable(stage);
}

export const PLATFORMS = ["Any short-video feed", "TikTok", "Instagram Reels", "YouTube Shorts", "Facebook Reels"];
export const AUDIENCES = ["General audience", "Curious learners", "Existing followers", "Buyers comparing options"];
export const DEFAULT_OBJECTIVE = "Keep an unfamiliar viewer watching and understanding the video without losing what already works.";
