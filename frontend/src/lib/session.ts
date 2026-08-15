/**
 * Frontend Session Manager with optimistic local caching.
 *
 * Operates in synergy with the backend's httpOnly cookie session:
 * 1. Synchronously reads cached user data from sessionStorage for instant,
 *    zero-flicker UI hydration across page navigations.
 * 2. Background-revalidates against GET /api/auth/me to ensure the server-side
 *    session is still valid.
 * 3. Immediately clears local cache and triggers logout/redirect if backend returns 401.
 */
import { apiFetch, ApiError } from "./api";
import { toast } from "./alerts";

export interface UserAllocation {
	ram_mb: number;
	cpu: number;
	disk_gb: number;
}

export interface UserSession {
	id: string;
	email: string;
	role: "admin" | "user" | string;
	status: string;
	quota_ram_mb: number;
	quota_cpu: number;
	quota_disk_gb: number;
	allocation?: UserAllocation;
	cachedAt?: number;
}

const SESSION_KEY = "hsm_frontend_session";

/** Synchronously retrieve cached session from sessionStorage */
export function getCachedSession(): UserSession | null {
	if (typeof window === "undefined") return null;
	try {
		const raw = sessionStorage.getItem(SESSION_KEY);
		if (!raw) return null;
		const parsed = JSON.parse(raw) as UserSession;
		return parsed;
	} catch {
		return null;
	}
}

/** Store user session in sessionStorage */
export function setCachedSession(session: UserSession): void {
	if (typeof window === "undefined") return;
	try {
		sessionStorage.setItem(SESSION_KEY, JSON.stringify({ ...session, cachedAt: Date.now() }));
	} catch {
		// ignore storage quota errors
	}
}

/** Remove cached session from storage */
export function clearCachedSession(): void {
	if (typeof window === "undefined") return;
	try {
		sessionStorage.removeItem(SESSION_KEY);
	} catch {
		// ignore
	}
}

/**
 * Validates session seamlessly:
 * - If cached session exists, calls `onSuccess` immediately (instant paint).
 * - Background re-validates against GET /api/auth/me to keep server authority.
 * - On 401 unauthorized, clears cache and executes `onUnauthorized` (or redirects).
 */
export async function syncSession(options?: {
	onSuccess?: (session: UserSession) => void;
	onUnauthorized?: () => void;
	onError?: (err: unknown) => void;
}): Promise<UserSession | null> {
	const cached = getCachedSession();

	if (cached && options?.onSuccess) {
		options.onSuccess(cached);
	}

	try {
		const fresh = await apiFetch<UserSession>("/api/auth/me");
		setCachedSession(fresh);
		if (options?.onSuccess) {
			options.onSuccess(fresh);
		}
		return fresh;
	} catch (err) {
		if (err instanceof ApiError && err.status === 401) {
			if (cached) {
				toast.warning("Your session has expired. Please sign in again.", "Session Expired");
			}
			clearCachedSession();
			if (options?.onUnauthorized) {
				options.onUnauthorized();
			}
			return null;
		}

		// If backend was unreachable but we have cached session, stay in cached session
		if (cached) {
			return cached;
		}

		if (options?.onError) {
			options.onError(err);
		}
		return null;
	}
}
