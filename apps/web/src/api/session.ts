let locked = false;
const listeners = new Set<() => void>();
export const sessionIsLocked = () => locked;
export function subscribeSession(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; }
export function markSessionLocked(value = true) { locked = value; listeners.forEach((listener) => listener()); }

/** Exchanges the transient credential for a server-owned HttpOnly session cookie. */
export async function unlockSession(token: string): Promise<void> {
  const response = await fetch("/api/session", { method: "POST", headers: { Authorization: `Bearer ${token.trim()}`, Accept: "application/json" } });
  if (!response.ok) throw new Error(response.status === 401 ? "The access token was not accepted." : `Could not unlock this session (HTTP ${response.status}).`);
  const receipt: unknown = await response.json();
  if (!receipt || typeof receipt !== "object" || !("authenticated" in receipt) || receipt.authenticated !== true) throw new Error("The server did not confirm authentication.");
  markSessionLocked(false);
}
