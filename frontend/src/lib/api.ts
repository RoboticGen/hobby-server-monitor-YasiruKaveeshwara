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
const API_BASE_URL: string =
  import.meta.env.PUBLIC_API_BASE_URL ?? "http://localhost:8000";

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
 * Perform a JSON request against the backend and return the parsed body.
 *
 * @param path  Backend path beginning with a slash, e.g. "/api/auth/me".
 * @param options  Standard fetch options; `headers` and `body` are merged.
 * @returns The parsed JSON response body, typed as T.
 * @throws {ApiError} When the response status is outside the 2xx range.
 */
export async function apiFetch<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
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

  if (!response.ok) {
    // Read the backend's own error text where it exists. A failed parse must
    // not mask the real HTTP status, so the fallback is an empty body rather
    // than a thrown SyntaxError.
    const body: FalconErrorBody = await response.json().catch(() => ({}));
    throw new ApiError(
      response.status,
      body.title ?? response.statusText,
      body.description ?? "",
    );
  }

  // 204 No Content carries no body; calling .json() on it throws. Callers of
  // such endpoints type T as void and receive undefined.
  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}
