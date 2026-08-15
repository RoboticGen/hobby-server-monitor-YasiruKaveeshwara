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
import { apiFetch } from "../lib/api";
import { clearCachedSession } from "../lib/session";
import { toast, showConfirm } from "../lib/alerts";

/** Response body of POST /api/auth/logout (backend/resources/auth.py). */
interface LogoutResponse {
	message: string;
}

export default function LogoutButton() {
	const [pending, setPending] = useState<boolean>(false);

	async function performLogout(): Promise<void> {
		setPending(true);
		toast.info("Terminating session and signing out…");

		try {
			await apiFetch<LogoutResponse>("/api/auth/logout", { method: "POST" });
		} catch {
			// Even if server returns 401 or network error, clear local session
		} finally {
			clearCachedSession();
			toast.success("Signed out successfully.");
			window.location.assign("/login");
		}
	}

	async function handleLogout(): Promise<void> {
		await showConfirm({
			title: "Sign Out",
			message: "Are you sure you want to end your active session? You will be redirected to the sign-in page.",
			confirmText: "Sign Out",
			cancelText: "Cancel",
			isDanger: false,
			iconType: "info",
			onConfirm: () => performLogout(),
		});
	}

	return (
		<button type='button' onClick={handleLogout} disabled={pending}>
			{pending ? "Signing out…" : "Sign out"}
		</button>
	);
}
