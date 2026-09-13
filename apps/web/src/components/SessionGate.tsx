import { useState, useSyncExternalStore, type ReactNode } from "react";
import { markSessionLocked, sessionIsLocked, subscribeSession, unlockSession } from "../api/session";

export function SessionGate({ children }: { children: ReactNode }) {
  const locked = useSyncExternalStore(subscribeSession, sessionIsLocked);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unlockedHere, setUnlockedHere] = useState(false);
  const unlock = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy || !token.trim()) return;
    const transient = token;
    setToken(""); setBusy(true); setError(null);
    try { await unlockSession(transient); setUnlockedHere(true); }
    catch (err) { setError(err instanceof Error ? err.message : "Could not unlock this session."); }
    finally { setBusy(false); }
  };
  const lock = async () => {
    try {
      const response = await fetch("/api/session", { method: "DELETE" });
      if (!response.ok) throw new Error("Could not end the server session. Try again.");
      setUnlockedHere(false); markSessionLocked();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not lock this session."); }
  };
  if (!locked) return <>{unlockedHere ? <div className="session-lock"><button className="btn btn-quiet btn-small" onClick={() => void lock()}>Lock session</button>{error ? <span role="alert">{error}</span> : null}</div> : null}{children}</>;
  return <main className="page session-gate"><span className="eyebrow">DirectorLoop · Private studio</span><h1 className="editorial-title">Unlock this session.</h1><p>Enter the studio access token to open the protected videos and experiments.</p><form onSubmit={(event) => void unlock(event)}><label className="setting"><span className="field-label">Studio access token</span><input type="password" value={token} autoComplete="off" autoCapitalize="none" spellCheck={false} onChange={(event) => setToken(event.target.value)} disabled={busy} required /></label><button className="btn btn-primary" disabled={busy || !token.trim()}>{busy ? "Unlocking" : "Open studio"}</button></form>{error ? <p className="callout callout-bad" role="alert">{error}</p> : null}<p className="muted small">The token is exchanged for a private browser session and is not stored in browser storage.</p></main>;
}
