import { useState } from "react";
import { getClient } from "../api/client";
import type { ReviewAssignment } from "../api/types";
import { errorMessage } from "../lib/format";
import { Badge } from "./Badge";

interface Props {
  token: string;
  assignment: ReviewAssignment;
}

function participantId(): string {
  const key = "dl_participant_id";
  try {
    const existing = localStorage.getItem(key);
    if (existing) return existing;
    const fresh = `p_${Math.random().toString(36).slice(2, 10)}`;
    localStorage.setItem(key, fresh);
    return fresh;
  } catch {
    return `p_${Math.random().toString(36).slice(2, 10)}`;
  }
}

export function HumanReviewPanel({ token, assignment }: Props) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [consented, setConsented] = useState(false);
  const [confusion, setConfusion] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mediaFailed, setMediaFailed] = useState(false);

  const complete = assignment.questions.every((q) => answers[q.id]);

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const client = await getClient();
      const confusionMs = confusion.trim() === "" ? null : Math.round(parseFloat(confusion) * 1000);
      await client.submitReview(token, {
        participant_id: participantId(),
        answers,
        confusion_ms: Number.isFinite(confusionMs as number) ? confusionMs : null,
        consented: true,
      });
      setDone(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  if (done) {
    return (
      <section className="panel review">
        <h2>Thank you</h2>
        <p>Your answers were recorded with a pseudonymous id. You can close this page.</p>
      </section>
    );
  }

  return (
    <section className="panel review" aria-label="Blinded review">
      <div className="panel-head">
        <h2>Watch, then answer</h2>
        <Badge kind="human" />
      </div>
      <p className="consent">{assignment.consent_text}</p>
      <label className="check">
        <input type="checkbox" checked={consented} onChange={(e) => setConsented(e.target.checked)} /> I agree to take part.
      </label>
      {consented ? (
        <>
          {mediaFailed ? (
            <div className="player-empty" role="alert">
              The video could not be loaded.
            </div>
          ) : (
            <video className="player-video review-video" src={assignment.media_url} controls playsInline onError={() => setMediaFailed(true)} />
          )}
          <ol className="review-questions">
            {assignment.questions.map((q) => (
              <li key={q.id} className="review-q">
                <div className="review-q-text">{q.text}</div>
                <div className="review-options" role="radiogroup" aria-label={q.text}>
                  {q.options.map((o) => (
                    <label key={o.id} className={`option ${answers[q.id] === o.id ? "option-selected" : ""}`}>
                      <input
                        type="radio"
                        name={q.id}
                        value={o.id}
                        checked={answers[q.id] === o.id}
                        onChange={() => setAnswers((prev) => ({ ...prev, [q.id]: o.id }))}
                      />
                      {o.text}
                    </label>
                  ))}
                </div>
              </li>
            ))}
          </ol>
          <label className="intake-field">
            <span className="muted small">Optional: at what second were you confused, if at all?</span>
            <input className="input" inputMode="decimal" value={confusion} onChange={(e) => setConfusion(e.target.value)} placeholder="e.g. 2.5" />
          </label>
          <button type="button" className="btn btn-improve" onClick={submit} disabled={!complete || submitting}>
            {submitting ? "Submitting" : "Submit answers"}
          </button>
          {error ? (
            <div className="bad mono small" role="alert">
              {error}
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
