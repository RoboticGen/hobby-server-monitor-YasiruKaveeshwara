/**
 * Grant / revoke one user's access to one container.
 *
 * Calls POST /api/users/{user_id}/containers/{container_id} to grant and
 * DELETE on the same path to revoke, then reports the server's verdict.
 */
import { useState } from "react";
import { apiFetch, ApiError } from "../lib/api";

interface GrantResult {
	assignment_id: string;
	user_id: string;
	container_id: string;
}

interface RevokeResult {
	user_id: string;
	container_id: string;
	message: string;
}

export type AssignmentState = "unknown" | "granted" | "revoked";

export interface AssignmentToggleProps {
	userId: string;
	containerId: string;
	containerName: string;
	initialState?: AssignmentState;
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

	const path = `/api/users/${encodeURIComponent(userId)}/containers/${encodeURIComponent(containerId)}`;

	function resolve(next: AssignmentState, changed: boolean, message: string): void {
		setState(next);
		setNotice(message);
		onResolved?.(next, changed);
	}

	async function handleGrant(): Promise<void> {
		setBusy(true);
		setError(null);
		setNotice(null);

		try {
			await apiFetch<GrantResult>(path, { method: "POST" });
			resolve("granted", true, `Granted access to ${containerName}.`);
		} catch (err) {
			if (err instanceof ApiError && err.status === 409) {
				resolve("granted", false, `Already assigned to ${containerName}.`);
			} else if (err instanceof ApiError) {
				setError(`Could not grant: ${err.message}`);
			} else {
				setError("Could not reach the server to grant access.");
			}
		} finally {
			setBusy(false);
		}
	}

	async function handleRevoke(): Promise<void> {
		setBusy(true);
		setError(null);
		setNotice(null);

		try {
			await apiFetch<RevokeResult>(path, { method: "DELETE" });
			resolve("revoked", true, `Revoked access to ${containerName}.`);
		} catch (err) {
			if (err instanceof ApiError && err.status === 404) {
				resolve("revoked", false, `Not currently assigned to ${containerName}.`);
			} else if (err instanceof ApiError) {
				setError(`Could not revoke: ${err.message}`);
			} else {
				setError("Could not reach the server to revoke access.");
			}
		} finally {
			setBusy(false);
		}
	}

	const isGranted = state === "granted";
	const isRevoked = state === "revoked";

	return (
		<div className={`assignment-item ${state}`}>
			<div className='item-info'>
				<span className='container-badge-icon'>📦</span>
				<span className='container-name-text'>{containerName}</span>
				{state !== "unknown" && (
					<span className={`status-badge ${isGranted ? "running" : "stopped"}`}>
						{isGranted ? "Assigned" : "No Access"}
					</span>
				)}
			</div>

			<div className='item-controls'>
				<button
					type='button'
					className={`btn-sm ${isGranted ? "active-grant" : ""}`}
					onClick={() => void handleGrant()}
					disabled={busy || isGranted}
					title={`Grant access to ${containerName}`}>
					{busy ? "…" : "Grant"}
				</button>
				<button
					type='button'
					className={`btn-sm danger ${isRevoked ? "active-revoke" : ""}`}
					onClick={() => void handleRevoke()}
					disabled={busy || isRevoked}
					title={`Revoke access to ${containerName}`}>
					{busy ? "…" : "Revoke"}
				</button>
			</div>

			{error && <span className='toggle-error'>{error}</span>}
			{notice && <span className='toggle-notice'>{notice}</span>}
		</div>
	);
}
