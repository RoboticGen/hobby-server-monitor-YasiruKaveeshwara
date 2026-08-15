/**
 * Grant / revoke one user's access to one container.
 *
 * Calls POST /api/users/{user_id}/containers/{container_id} to grant and
 * DELETE on the same path to revoke, then reports the server's verdict.
 */
import { useState } from "react";
import { apiFetch, ApiError } from "../lib/api";
import { toast } from "../lib/alerts";

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
			const msg = `Granted access to ${containerName}.`;
			resolve("granted", true, msg);
			toast.success(msg, "Container Assigned");
		} catch (err) {
			if (err instanceof ApiError && err.status === 409) {
				const msg = `Already assigned to ${containerName}.`;
				resolve("granted", false, msg);
				toast.info(msg);
			} else if (err instanceof ApiError) {
				const errMsg = `Could not grant: ${err.message}`;
				setError(errMsg);
				toast.error(err, "Assignment Failed");
			} else {
				const errMsg = "Could not reach the server to grant access.";
				setError(errMsg);
				toast.error(errMsg, "Assignment Failed");
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
			const msg = `Revoked access to ${containerName}.`;
			resolve("revoked", true, msg);
			toast.success(msg, "Container Revoked");
		} catch (err) {
			if (err instanceof ApiError && err.status === 404) {
				const msg = `Not currently assigned to ${containerName}.`;
				resolve("revoked", false, msg);
				toast.info(msg);
			} else if (err instanceof ApiError) {
				const errMsg = `Could not revoke: ${err.message}`;
				setError(errMsg);
				toast.error(err, "Revocation Failed");
			} else {
				const errMsg = "Could not reach the server to revoke access.";
				setError(errMsg);
				toast.error(errMsg, "Revocation Failed");
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
