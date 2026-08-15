/**
 * Inline role/status editor for one user.
 *
 * Mounted on the admin users page inside that user's manage panel.
 */
import { useState, type SyntheticEvent } from "react";
import { apiFetch, ApiError } from "../lib/api";
import { toast, showConfirm } from "../lib/alerts";

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
			toast.info("No changes to save.");
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
			const msg = nextStatus === "revoked" ? `${email} access revoked.` : "Account privileges updated.";
			setSaved(msg);
			toast.success(msg, "User Updated");
		} catch (err) {
			const errMsg = err instanceof ApiError ? err.message : "Could not reach the server to save the user.";
			setError(errMsg);
			toast.error(err, "Failed to Update User");
		} finally {
			setSaving(false);
		}
	}

	async function handleRevoke(): Promise<void> {
		await showConfirm({
			title: "Revoke User Access",
			message: `Revoke access for ${email}? They will immediately lose access and active sessions will be terminated.`,
			confirmText: "Revoke Access",
			cancelText: "Cancel",
			isDanger: true,
			iconType: "danger",
			onConfirm: () => submitPatch(undefined, "revoked"),
		});
	}

	return (
		<form className='account-editor-panel' onSubmit={(event) => void submitPatch(event)}>
			<h4>Account & Role Privileges</h4>

			{error && <div className='alert alert-error'>{error}</div>}
			{saved && <div className='alert alert-success'>{saved}</div>}

			<div className='editor-fields'>
				<div className='form-group'>
					<label htmlFor={`user-role-${userId}`}>System Role</label>
					<select
						id={`user-role-${userId}`}
						value={currentRole}
						onChange={(e) => setCurrentRole(e.target.value as UserRole)}
						disabled={saving}>
						<option value='user'>Regular User</option>
						<option value='admin'>Administrator</option>
					</select>
				</div>

				<div className='form-group'>
					<label htmlFor={`user-status-${userId}`}>Account Status</label>
					<select
						id={`user-status-${userId}`}
						value={currentStatus}
						onChange={(e) => setCurrentStatus(e.target.value as UserStatus)}
						disabled={saving}>
						<option value='active'>Active</option>
						<option value='invited'>Invited</option>
						<option value='revoked'>Revoked</option>
					</select>
				</div>
			</div>

			<div className='editor-actions'>
				<button type='submit' className='primary' disabled={!dirty || saving}>
					{saving ? "Saving…" : "Save Role & Status"}
				</button>
				{currentStatus !== "revoked" && (
					<button type='button' className='danger' onClick={() => void handleRevoke()} disabled={saving}>
						Revoke Account
					</button>
				)}
			</div>
		</form>
	);
}
