/**
 * Sign-out control.
 *
 * An island rather than a plain link because logging out is a POST that must
 * revoke the session server-side (the backend deletes the session row, so a
 * captured cookie stops working), and only JavaScript can issue that POST and
 * react to the result. The login link is the mirror image of this: a plain
 * navigation, because it has to leave our origin entirely.
 */
import { useState } from "react";
import { apiFetch, ApiError } from "../lib/api";

/** Response body of POST /api/auth/logout (backend/resources/auth.py). */
interface LogoutResponse {
  message: string;
}

export default function LogoutButton() {
  // Disables the button while the request is in flight, so an impatient
  // double-click cannot fire a second logout against an already-revoked
  // session and surface a confusing error.
  const [pending, setPending] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  /**
   * Revoke the session, then navigate to the login page.
   *
   * Navigation uses a full page load (`location.assign`) rather than a
   * client-side route change so that no state from the signed-in session
   * survives in memory after sign-out.
   */
  async function handleLogout(): Promise<void> {
    setPending(true);
    setError(null);

    try {
      await apiFetch<LogoutResponse>("/api/auth/logout", { method: "POST" });
      window.location.assign("/login");
    } catch (err) {
      // Failing to reach the backend means the session may still be live, so
      // the user is told rather than being sent to /login as if it worked.
      setError(
        err instanceof ApiError
          ? `Sign out failed (${err.status}): ${err.message}`
          : "Sign out failed: could not reach the server.",
      );
      setPending(false);
    }
  }

  return (
    <>
      <button type="button" onClick={handleLogout} disabled={pending}>
        {pending ? "Signing out…" : "Sign out"}
      </button>
      {/* role="alert" so screen readers announce the failure, which is
          otherwise a silent visual-only change. */}
      {error && <p role="alert">{error}</p>}
    </>
  );
}
