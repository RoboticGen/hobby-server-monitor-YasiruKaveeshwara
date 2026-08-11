/**
 * Invite-a-user form (admin only).
 *
 * Mounted on the admin users page. Collects an email, a role, and the initial
 * quota, then POSTs to /api/users and reports the server's own verdict.
 *
 * "Invite" sends nothing. The backend creates the row with status='invited'
 * and authentication is Google-only, so the person simply signs in with that
 * Google address and the OAuth callback upgrades them to 'active' on first
 * login. The copy below says so out loud, because a form labelled "invite"
 * that quietly sends no email otherwise reads as broken.
 */
import { useState, type SyntheticEvent } from "react";
import { apiFetch, ApiError } from "../lib/api";

/** The only two roles the backend accepts; anything else is a 400. */
type UserRole = "admin" | "user";

/** Response body of POST /api/users on success (201). */
interface InvitedUser {
	id: string;
	email: string;
	role: UserRole;
	status: string;
}

export interface InviteUserFormProps {
	/**
	 * Called after a user is invited successfully, so the page can re-fetch the
	 * list. Optional because the form is useful (and testable) without a parent
	 * that cares about the result.
	 */
	onInvited?: () => void;
}

/**
 * Quota values the form opens with.
 *
 * These mirror the example in the backend's own `on_post` docstring rather
 * than the database column defaults, which are 0. Zero means *unlimited* —
 * backend/lxd/quota.py skips the check entirely for a resource whose quota is
 * 0 — so opening at the column defaults would make "invite someone and leave
 * the quota fields alone" silently grant unrestricted use of the host, which
 * is the opposite of what a quota screen should do by accident. Unlimited is
 * still one keystroke away, and the hint below says so.
 */
const DEFAULT_QUOTA_RAM_MB = 2048;
const DEFAULT_QUOTA_CPU = 2;
const DEFAULT_QUOTA_DISK_GB = 20;

export default function InviteUserForm({ onInvited }: InviteUserFormProps) {
	// --- Form fields ---
	const [email, setEmail] = useState<string>("");
	const [role, setRole] = useState<UserRole>("user");
	const [quotaRamMb, setQuotaRamMb] = useState<number>(DEFAULT_QUOTA_RAM_MB);
	const [quotaCpu, setQuotaCpu] = useState<number>(DEFAULT_QUOTA_CPU);
	const [quotaDiskGb, setQuotaDiskGb] = useState<number>(DEFAULT_QUOTA_DISK_GB);

	// --- Request lifecycle ---
	const [submitting, setSubmitting] = useState<boolean>(false);
	const [error, setError] = useState<string | null>(null);
	const [success, setSuccess] = useState<string | null>(null);

	/**
	 * Submit the invite.
	 *
	 * `preventDefault` first, because the default form action is a full page
	 * navigation that would throw away this island and the session-checked page
	 * around it.
	 *
	 * The email is sent as typed. The address format is not re-validated here
	 * beyond the browser's own `type="email"` check: the backend lowercases and
	 * validates it, owns the uniqueness constraint, and answers 409 for a
	 * duplicate — a rule this form cannot evaluate at all, since it does not
	 * know who already exists. Re-implementing any of that would create a
	 * second copy to keep in sync.
	 *
	 * Both failure paths end in the same place on purpose: whatever the server
	 * said is shown verbatim. Its wording names the actual problem ("A user
	 * with email 'x' already exists", "A valid email address is required"),
	 * which a generic message would discard.
	 */
	async function handleSubmit(event: SyntheticEvent<HTMLFormElement>) {
		event.preventDefault();
		setSubmitting(true);
		setError(null);
		setSuccess(null);

		try {
			const invited = await apiFetch<InvitedUser>("/api/users", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					email,
					role,
					quota_ram_mb: quotaRamMb,
					quota_cpu: quotaCpu,
					quota_disk_gb: quotaDiskGb,
				}),
			});

			// Names the resulting status explicitly, so the admin is not left
			// wondering why the new row does not say "active" like the others.
			setSuccess(
				`Invited ${invited.email} as ${invited.role}. They stay "${invited.status}" until they sign in with that Google account.`,
			);

			// Only the email is cleared: it is the one field unique to this
			// invite, while the role and quota are usually the same across a
			// batch of people being onboarded together.
			setEmail("");
			onInvited?.();
		} catch (err) {
			setError(err instanceof ApiError ? err.message : "Could not reach the server to send the invite.");
		} finally {
			// In `finally` so the button re-enables on the failure path too;
			// otherwise one rejected invite would leave the form permanently
			// stuck on "Inviting…".
			setSubmitting(false);
		}
	}

	return (
		<form className='invite-form' onSubmit={handleSubmit}>
			<h2>Invite a user</h2>

			<label htmlFor='invite-email'>Email</label>
			<input
				id='invite-email'
				name='email'
				type='email'
				value={email}
				required
				onChange={(event) => setEmail(event.target.value)}
				placeholder='person@example.com'
			/>
			<p className='hint'>Must be the address of the Google account they will sign in with.</p>

			<label htmlFor='invite-role'>Role</label>
			<select id='invite-role' name='role' value={role} onChange={(event) => setRole(event.target.value as UserRole)}>
				<option value='user'>User — sees only assigned containers</option>
				<option value='admin'>Admin — full access, quotas not enforced</option>
			</select>

			{/* Quota fields are `required` for a reason that is easy to miss:
			    an empty number input reads back as "", and Number("") is 0 —
			    which this API defines as *unlimited*. Without `required`,
			    clearing a field to retype it and submitting early would hand
			    out unrestricted resources instead of raising an error. */}
			<label htmlFor='invite-ram'>RAM quota (MB)</label>
			<input
				id='invite-ram'
				name='quota_ram_mb'
				type='number'
				min={0}
				step={256}
				value={quotaRamMb}
				required
				onChange={(event) => setQuotaRamMb(Number(event.target.value))}
			/>

			<label htmlFor='invite-cpu'>CPU quota (cores)</label>
			<input
				id='invite-cpu'
				name='quota_cpu'
				type='number'
				min={0}
				step={0.5}
				value={quotaCpu}
				required
				onChange={(event) => setQuotaCpu(Number(event.target.value))}
			/>

			<label htmlFor='invite-disk'>Disk quota (GB)</label>
			<input
				id='invite-disk'
				name='quota_disk_gb'
				type='number'
				min={0}
				step={1}
				value={quotaDiskGb}
				required
				onChange={(event) => setQuotaDiskGb(Number(event.target.value))}
			/>

			<p className='hint'>
				A quota of 0 means unlimited. Quotas cap the total across every container assigned to the user, not each
				container.
			</p>

			<button type='submit' disabled={submitting}>
				{submitting ? "Inviting…" : "Send invite"}
			</button>

			{/* role="alert" so a rejected invite is announced rather than just
			    drawn; the success line is role="status" because it is not an
			    interruption. Both carry the server's own wording. */}
			{error && (
				<p className='error' role='alert'>
					{error}
				</p>
			)}
			{success && (
				<p className='success' role='status'>
					{success}
				</p>
			)}
		</form>
	);
}
