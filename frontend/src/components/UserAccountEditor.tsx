/**
 * Inline role/status editor for one user.
 *
 * Mounted on the admin users page inside that user's manage panel. It lets
 * an admin change the user's role and status, including a soft revoke via the
 * backend PATCH endpoint.
 */
import { useState, type SyntheticEvent } from "react";
import { apiFetch, ApiError } from "../lib/api";

type UserRole = "admin" | "user";
type UserStatus = "invited" | "active" | "revoked";

interface UpdatedUser {
	id: string;
	email: string;
	role: UserRole;
	status: UserStatus;
	quota_ram_mb: number;
	quota_cpu: number;
	quota_disk_gb: number;
}

export interface UserAccountEditorProps {
	userId: string;
	email: string;
	role: UserRole;
	status: UserStatus;
	onSaved?: () => void;
}

export default function UserAccountEditor({ userId, email, role, status, onSaved }: UserAccountEditorProps) {
	const [currentRole, setCurrentRole] = useState<UserRole>(role);
	const [currentStatus, setCurrentStatus] = useState<UserStatus>(status);
	const [baselineRole, setBaselineRole] = useState<UserRole>(role);
	const [baselineStatus, setBaselineStatus] = useState<UserStatus>(status);
	const [saving, setSaving] = useState<boolean>(false);
	const [error, setError] = useState<string | null>(null);
	const [saved, setSaved] = useState<string | null>(null);

	const dirty = currentRole !== baselineRole || currentStatus !== baselineStatus;

	async function submitPatch(event?: SyntheticEvent<HTMLFormElement>, nextStatus?: UserStatus): Promise<void> {
		event?.preventDefault();
		setSaving(true);
		setError(null);
		setSaved(null);

		const effectiveStatus = nextStatus ?? currentStatus;
		const patch: Record<string, UserRole | UserStatus> = {};

		if (currentRole !== baselineRole) {
			patch.role = currentRole;
		}
		if (effectiveStatus !== baselineStatus || nextStatus !== undefined) {
			patch.status = effectiveStatus;
		}

		if (Object.keys(patch).length === 0) {
			setSaving(false);
			setSaved("No changes to save.");
			return;
		}

		try {
			const updated = await apiFetch<UpdatedUser>(`/api/users/${encodeURIComponent(userId)}`, {
				method: "PATCH",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(patch),
			});

			setCurrentRole(updated.role);
			setCurrentStatus(updated.status);
			setBaselineRole(updated.role);
			setBaselineStatus(updated.status);
			onSaved?.();
			setSaved(nextStatus === "revoked" ? `${email} has been revoked.` : "User settings saved.");
		} catch (err) {
			setError(err instanceof ApiError ? err.message : "Could not reach the server to save the user.");
		} finally {
			setSaving(false);
		}
	}

	async function handleRevoke(): Promise<void> {
		if (!window.confirm(`Revoke ${email}? They will no longer be able to sign in.`)) {
			return;
		}

		await submitPatch(undefined, "revoked");
	}

	return (
		<form className='account-editor' onSubmit={(event) => void submitPatch(event)}>
			<h3>Account access — {email}</h3>

			<label htmlFor={`user-role-${userId}`}>Role</label>
			<select
				id={`user-role-${userId}`}
				value={currentRole}
				onChange={(event) => setCurrentRole(event.target.value as UserRole)}
				disabled={saving}>
				<option value='user'>User</option>
				<option value='admin'>Admin</option>
			</select>

			<label htmlFor={`user-status-${userId}`}>Status</label>
			<select
				id={`user-status-${userId}`}
				value={currentStatus}
				onChange={(event) => setCurrentStatus(event.target.value as UserStatus)}
				disabled={saving}>
				<option value='invited'>Invited</option>
				<option value='active'>Active</option>
				<option value='revoked'>Revoked</option>
			</select>

			<button type='submit' disabled={saving || !dirty}>
				{saving ? "Saving…" : "Save account changes"}
			</button>
			<button
				type='button'
				className='danger'
				onClick={() => void handleRevoke()}
				disabled={saving || currentStatus === "revoked"}>
				Revoke user
			</button>

			<p className='hint'>Revoked users keep their row for audit history, but cannot sign in.</p>

			{error && (
				<p className='error' role='alert'>
					{error}
				</p>
			)}
			{saved && !error && (
				<p className='success' role='status'>
					{saved}
				</p>
			)}
		</form>
	);
}
