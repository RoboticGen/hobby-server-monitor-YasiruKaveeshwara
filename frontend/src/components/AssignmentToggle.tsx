/**
 * Grant / revoke one user's access to one container.
 *
 * Calls POST /api/users/{user_id}/containers/{container_id} to grant and
 * DELETE on the same path to revoke, then reports the server's verdict.
 *
 * WHY TWO BUTTONS INSTEAD OF A CHECKBOX:
 * A checkbox has to be drawn either ticked or unticked, which claims to know
 * whether the assignment already exists. Nothing in the API tells us that.
 * GET /api/containers scopes to the *caller's* own assignments, GET /api/users
 * returns allocation totals, and GET /api/accounting gives a container_count
 * without naming the containers — so for an arbitrary user there is no way to
 * read their assignment list. A checkbox rendered from a guess would show the
 * admin a state the server never confirmed.
 *
 * So this starts in an explicit "unknown" state and offers both actions. The
 * backend's answers are unambiguous enough to settle it from a single click:
 * 409 "Already assigned" means it was granted all along, and 404 "Assignment
 * not found" means it was not. After any call the state is known, and the
 * label says which.
 */
import { useState } from "react";
import { apiFetch, ApiError } from "../lib/api";

/** Response body of POST …/containers/{id} on success (201). */
interface GrantResult {
	assignment_id: string;
	user_id: string;
	container_id: string;
}

/** Response body of DELETE …/containers/{id} on success. */
interface RevokeResult {
	user_id: string;
	container_id: string;
	message: string;
}

/**
 * What we believe about this assignment.
 *
 * "unknown" is the honest starting point, not a loading state — see the note
 * at the top of the file. It is left behind for good once any call returns.
 *
 * Exported because the page holds the resolved value across re-renders and
 * hands it back in as `initialState`.
 */
export type AssignmentState = "unknown" | "granted" | "revoked";

export interface AssignmentToggleProps {
	/** User UUID — the {user_id} in the path. */
	userId: string;
	/** Container UUID — the {container_id} in the path. */
	containerId: string;
	/** Container name, so the control reads as a container and not a UUID. */
	containerName: string;
	/**
	 * State already learned for this pair, if the page has seen a call for it.
	 *
	 * Needed because a grant changes the user's allocation, so the page
	 * re-fetches and repaints the row — which remounts this component. Without
	 * a way back in, every refresh would forget what the server just told us
	 * and drop the control back to "unknown".
	 */
	initialState?: AssignmentState;
	/**
	 * Called whenever the server settles this assignment's state.
	 *
	 * `changed` separates "the assignment moved" (201 / 200, so the quota
	 * figures on the page are now stale) from "it was already like that"
	 * (409 / 404, where nothing moved and a re-fetch would be wasted work).
	 * Optional because the toggle works, and is testable, on its own.
	 */
	onResolved?: (state: AssignmentState, changed: boolean) => void;
}

export default function AssignmentToggle({
	userId,
	containerId,
	containerName,
	initialState = "unknown",
	onResolved,
}: AssignmentToggleProps) {
	const [state, setState] = useState<AssignmentState>(initialState);
	const [busy, setBusy] = useState<boolean>(false);
	const [error, setError] = useState<string | null>(null);
	const [notice, setNotice] = useState<string | null>(null);

	/** Path shared by both verbs; encoded because both ids land in the URL. */
	const path = `/api/users/${encodeURIComponent(userId)}/containers/${encodeURIComponent(containerId)}`;

	/** Record the settled state locally and tell the page, in that order. */
	function resolve(next: AssignmentState, changed: boolean, message: string): void {
		setState(next);
		setNotice(message);
		onResolved?.(next, changed);
	}

	/**
	 * Grant access.
	 *
	 * The 409 branch is the load-bearing one: the backend answers "Already
	 * assigned" when an active assignment exists, which is not a failure the
	 * admin caused — it is the answer to the question the button was asking.
	 * So it settles the state as "granted" and says so plainly instead of
	 * showing a red error for a container the user already has.
	 *
	 * Every other rejection is shown verbatim. That matters most for the quota
	 * refusal, where lxd/quota.py names the arithmetic — "Would exceed RAM
	 * quota by 512MB (current: 2048MB + requested: 512MB = 2560MB, quota:
	 * 2048MB)". Replacing that with a generic message would throw away the one
	 * thing the admin needs in order to decide what to change.
	 */
	async function handleGrant() {
		setBusy(true);
		setError(null);
		setNotice(null);

		try {
			await apiFetch<GrantResult>(path, { method: "POST" });
			resolve("granted", true, "Access granted.");
		} catch (err) {
			if (err instanceof ApiError && err.status === 409) {
				resolve("granted", false, "Already had access.");
			} else if (err instanceof ApiError) {
				setError(err.message);
			} else {
				setError("Could not reach the server to grant access.");
			}
		} finally {
			// In `finally` so a refused grant re-enables the buttons rather than
			// leaving the control stuck mid-request.
			setBusy(false);
		}
	}

	/**
	 * Revoke access.
	 *
	 * Mirror image of the grant path: a 404 here is the endpoint's way of
	 * saying there was no active assignment to remove, so the outcome the
	 * admin wanted already holds. Treating it as an error would be misleading.
	 *
	 * Revoking is a soft revoke — the backend sets active=0 and leaves the
	 * container running, so this frees the user's quota without touching the
	 * workload. The hint on the page says that, because "revoke" otherwise
	 * reads like it might delete something.
	 */
	async function handleRevoke() {
		setBusy(true);
		setError(null);
		setNotice(null);

		try {
			await apiFetch<RevokeResult>(path, { method: "DELETE" });
			resolve("revoked", true, "Access revoked.");
		} catch (err) {
			if (err instanceof ApiError && err.status === 404) {
				resolve("revoked", false, "Did not have access.");
			} else if (err instanceof ApiError) {
				setError(err.message);
			} else {
				setError("Could not reach the server to revoke access.");
			}
		} finally {
			setBusy(false);
		}
	}

	/**
	 * What the control currently claims about this assignment.
	 *
	 * Spelled out rather than implied by a tick, so "not checked yet" never
	 * gets read as "no access".
	 */
	const stateLabel =
		state === "granted" ? "Has access"
		: state === "revoked" ? "No access"
		: "Not known";

	return (
		<div className='assignment-toggle' data-state={state}>
			<span className='name'>{containerName}</span>
			<span className='state'>{stateLabel}</span>

			{/* Both buttons exist in every state, with the one matching the
			    current state disabled: the useless click is unavailable while
			    the useful one is always a single press away, and in the unknown
			    state either can be pressed to find out. */}
			<button type='button' onClick={handleGrant} disabled={busy || state === "granted"}>
				Grant
			</button>
			<button type='button' onClick={handleRevoke} disabled={busy || state === "revoked"}>
				Revoke
			</button>

			{/* role="alert" for rejections so they are announced, role="status"
			    for the quieter confirmations. The rejection text is the
			    backend's own, including the quota arithmetic. */}
			{error && (
				<p className='error' role='alert'>
					{error}
				</p>
			)}
			{notice && !error && (
				<p className='notice' role='status'>
					{notice}
				</p>
			)}
		</div>
	);
}
