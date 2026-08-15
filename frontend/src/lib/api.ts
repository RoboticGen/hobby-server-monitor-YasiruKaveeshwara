/**
 * Typed fetch wrapper for the Falcon backend.
 *
 * Every call the frontend makes to the backend goes through this module, so
 * the cookie and error-handling rules below are enforced in one place rather
 * than repeated (and eventually forgotten) at each call site.
 */

// The backend runs as a separate process on its own port, so requests are
// cross-origin in development. Reading the base URL from an env var keeps the
// dev (localhost:8000) and production (same-origin) setups on one code path.
// PUBLIC_ prefix is required by Astro for values that reach browser bundles.
const API_BASE_URL: string = import.meta.env.PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/**
 * Error thrown for any non-2xx backend response.
 *
 * Carries the HTTP status and the backend's own message rather than a generic
 * string, because the build guide requires pages to surface the server's exact
 * validation text (e.g. the container-name regex rejection) instead of
 * inventing their own copy of the rule.
 */
export class ApiError extends Error {
	readonly status: number;
	readonly title: string;

	constructor(status: number, title: string, description: string) {
		super(description || title || `Request failed with status ${status}`);
		this.name = "ApiError";
		this.status = status;
		this.title = title;
	}
}

/**
 * Shape of Falcon's default error body. Falcon serialises HTTPError responses
 * as `{"title": ..., "description": ...}`, so both fields are optional here
 * because a non-Falcon failure (a proxy 502, say) will not follow that shape.
 */
interface FalconErrorBody {
	title?: string;
	description?: string;
}

/**
 * Path of the refresh endpoint, named here so apiFetch can recognise it and
 * refuse to attempt a refresh-on-401 for the refresh call itself.
 */
const REFRESH_PATH = "/api/auth/refresh";

/**
 * The in-flight refresh, shared by every caller that hits a 401 at once.
 *
 * The dashboard mounts one polling tile per container, so an access token
 * expiring while the page is open produces a burst of simultaneous 401s. Each
 * firing its own refresh would send N identical requests for one new cookie,
 * and they would race to write it. Collapsing them onto one promise means the
 * first 401 refreshes and the rest await that same result.
 */
let refreshInFlight: Promise<boolean> | null = null;

/**
 * Ask the backend to reissue the access cookie from the refresh cookie.
 *
 * Uses bare fetch rather than apiFetch, both to avoid the recursion this
 * function exists to service and because a failed refresh is an expected
 * outcome (an expired or revoked session), not an error worth throwing.
 *
 * @returns true if a new access cookie was issued.
 */
function refreshAccessToken(): Promise<boolean> {
	if (refreshInFlight) return refreshInFlight;

	refreshInFlight = fetch(`${API_BASE_URL}${REFRESH_PATH}`, {
		method: "POST",
		credentials: "include",
		headers: { Accept: "application/json" },
	})
		.then((response) => response.ok)
		// A network failure here is indistinguishable to the caller from a
		// refused refresh: either way there is no usable session.
		.catch(() => false)
		.finally(() => {
			refreshInFlight = null;
		});

	return refreshInFlight;
}

/**
 * Perform a JSON request against the backend and return the parsed body.
 *
 * A 401 is retried once behind a token refresh. The access token is
 * deliberately short-lived, so without this every call site would need its own
 * expiry handling and a user would be logged out mid-task on a 30-minute
 * timer. Recovering here keeps that concern in one place, which is the same
 * reason the cookie rule lives here.
 *
 * @param path  Backend path beginning with a slash, e.g. "/api/auth/me".
 * @param options  Standard fetch options; `headers` and `body` are merged.
 * @returns The parsed JSON response body, typed as T.
 * @throws {ApiError} When the response status is outside the 2xx range.
 */
export async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
	// Issuing the request is a closure because a 401 needs it sent twice. Every
	// call site passes `body` as a JSON string, which is safe to replay; a
	// stream body would not be, since it cannot be read a second time.
	const send = (): Promise<Response> =>
		fetch(`${API_BASE_URL}${path}`, {
			...options,
			// The backend authenticates via httpOnly cookies set by the OAuth callback
			// (backend Phase 5 / decision 7.1). Those cookies are deliberately
			// unreadable from JavaScript, so the only way to authenticate a request is
			// to let the browser attach them itself — which cross-origin fetch does
			// *not* do by default. Without this line every authenticated call 401s.
			credentials: "include",
			headers: {
				Accept: "application/json",
				...options.headers,
			},
		});

	let response = await send();

	// One retry, and only for a path that is not itself the refresh call —
	// otherwise a rejected refresh would recurse. Calling send() directly
	// rather than re-entering apiFetch keeps that bound to a single attempt.
	if (response.status === 401 && path !== REFRESH_PATH) {
		if (await refreshAccessToken()) {
			response = await send();
		}
	}

	if (!response.ok) {
		// Read the backend's own error text where it exists. A failed parse must
		// not mask the real HTTP status, so the fallback is an empty body rather
		// than a thrown SyntaxError.
		const body: FalconErrorBody = await response.json().catch(() => ({}));
		throw new ApiError(response.status, body.title ?? response.statusText, body.description ?? "");
	}

	// 204 No Content carries no body; calling .json() on it throws. Callers of
	// such endpoints type T as void and receive undefined.
	if (response.status === 204) {
		return undefined as T;
	}

	return (await response.json()) as T;
}
