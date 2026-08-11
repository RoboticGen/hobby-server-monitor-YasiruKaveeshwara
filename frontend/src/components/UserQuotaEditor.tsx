/**
 * Inline quota editor for one user.
 *
 * Renders the three quota fields alongside what the user currently holds, and
 * PATCHes /api/users/{id} when they change.
 *
 * Mounted once per user by the admin users page, inside that user's manage
 * panel. It keeps the edited values in local state and treats the server's
 * response as the new truth, so the page never has to reach in and correct it.
 */
import { useState, type SyntheticEvent } from "react";
import { apiFetch, ApiError } from "../lib/api";

/** What the user's assigned containers currently add up to. */
export interface QuotaAllocation {
	ram_mb: number;
	cpu: number;
	disk_gb: number;
}

/** Response body of PATCH /api/users/{id} on success. */
interface UpdatedUser {
	id: string;
	email: string;
	role: string;
	status: string;
	quota_ram_mb: number;
	quota_cpu: number;
	quota_disk_gb: number;
}

/**
 * Only the quota fields are sent. `role` and `status` are also accepted by the
 * endpoint but are not edited here — no step has asked for those controls, and
 * silently including them would let this form overwrite values it never showed.
 */
interface QuotaPatch {
	quota_ram_mb?: number;
	quota_cpu?: number;
	quota_disk_gb?: number;
}

/** The three quota values as one unit, used for the last-known-saved baseline. */
interface QuotaValues {
	ramMb: number;
	cpu: number;
	diskGb: number;
}

export interface UserQuotaEditorProps {
	/** User UUID — the {id} in the PATCH path. */
	userId: string;
	/** Current RAM quota in MB (DB column quota_ram_mb). */
	quotaRamMb: number;
	/** Current CPU quota in cores (DB column quota_cpu). */
	quotaCpu: number;
	/** Current disk quota in GB (DB column quota_disk_gb). */
	quotaDiskGb: number;
	/** What the user's containers already consume, for the "used" figures. */
	allocation: QuotaAllocation;
	/**
	 * Called after a successful save so the page can re-fetch. Optional because
	 * the editor is complete (and testable) without a parent that reacts.
	 */
	onSaved?: () => void;
}

/** Drop trailing zeros so a CPU quota of 2.0 reads "2" and 1.5 stays "1.5". */
function formatNumber(value: number): string {
	return String(Number(value.toFixed(2)));
}

export default function UserQuotaEditor({
	userId,
	quotaRamMb,
	quotaCpu,
	quotaDiskGb,
	allocation,
	onSaved,
}: UserQuotaEditorProps) {
	// The values in the inputs right now.
	const [ramMb, setRamMb] = useState<number>(quotaRamMb);
	const [cpu, setCpu] = useState<number>(quotaCpu);
	const [diskGb, setDiskGb] = useState<number>(quotaDiskGb);

	// What the server last confirmed. Kept separately from the props because a
	// successful save moves it forward while the props stay at the values this
	// editor was mounted with — comparing against the props instead would leave
	// the form looking permanently unsaved after the first save.
	const [baseline, setBaseline] = useState<QuotaValues>({
		ramMb: quotaRamMb,
		cpu: quotaCpu,
		diskGb: quotaDiskGb,
	});

	const [saving, setSaving] = useState<boolean>(false);
	const [error, setError] = useState<string | null>(null);
	const [saved, setSaved] = useState<boolean>(false);

	// Save stays disabled until something actually differs. Without this,
	// clicking Save on an untouched form would send an empty patch and earn the
	// backend's "Nothing to update" 400 — a confusing error for doing nothing
	// wrong.
	const dirty = ramMb !== baseline.ramMb || cpu !== baseline.cpu || diskGb !== baseline.diskGb;

	/**
	 * Whether any entered quota sits below what the user already holds.
	 *
	 * The backend allows this: the update writes the columns without consulting
	 * current allocation, and check_quota only runs when something new is
	 * granted. So the result is a user who is over quota — their running
	 * containers are untouched, but the next grant is refused. That is a
	 * legitimate way to wind someone down, so this warns rather than blocks.
	 *
	 * The zero cases are excluded because 0 means unlimited, which can never be
	 * below anything.
	 */
	const belowAllocation =
		(ramMb > 0 && ramMb < allocation.ram_mb) ||
		(cpu > 0 && cpu < allocation.cpu) ||
		(diskGb > 0 && diskGb < allocation.disk_gb);

	/**
	 * Save the changed quota fields.
	 *
	 * `preventDefault` first: the default form action is a full page navigation
	 * that would tear down this island and the session-checked page around it.
	 *
	 * Only fields that actually changed are sent. Sending all three would
	 * overwrite the two the admin did not touch with the values this editor was
	 * mounted from, silently reverting a change made elsewhere in between.
	 *
	 * A rejection is shown verbatim, since the backend's wording names the real
	 * rule broken — "Quota must be a non-negative number".
	 */
	async function handleSubmit(event: SyntheticEvent<HTMLFormElement>) {
		event.preventDefault();
		setSaving(true);
		setError(null);
		setSaved(false);

		const patch: QuotaPatch = {};
		if (ramMb !== baseline.ramMb) patch.quota_ram_mb = ramMb;
		if (cpu !== baseline.cpu) patch.quota_cpu = cpu;
		if (diskGb !== baseline.diskGb) patch.quota_disk_gb = diskGb;

		try {
			const updated = await apiFetch<UpdatedUser>(`/api/users/${encodeURIComponent(userId)}`, {
				method: "PATCH",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(patch),
			});

			// Adopt the server's numbers rather than assuming ours took effect:
			// it validates and coerces (quota_ram_mb and quota_disk_gb are ints,
			// quota_cpu a float), so what it returns is the truth. Both the
			// inputs and the baseline move together, which is what clears the
			// dirty flag.
			setRamMb(updated.quota_ram_mb);
			setCpu(updated.quota_cpu);
			setDiskGb(updated.quota_disk_gb);
			setBaseline({
				ramMb: updated.quota_ram_mb,
				cpu: updated.quota_cpu,
				diskGb: updated.quota_disk_gb,
			});
			setSaved(true);
			onSaved?.();
		} catch (err) {
			setError(err instanceof ApiError ? err.message : "Could not reach the server to save the quota.");
		} finally {
			// In `finally` so a rejected save re-enables the button instead of
			// leaving the form stuck on "Saving…".
			setSaving(false);
		}
	}

	return (
		<form className='quota-editor' onSubmit={handleSubmit}>
			{/* Each field pairs the editable ceiling with what is already used,
			    so the admin can see the headroom they are changing rather than
			    a bare number with no context. */}
			<label htmlFor={`quota-ram-${userId}`}>RAM (MB)</label>
			<input
				id={`quota-ram-${userId}`}
				type='number'
				min={0}
				step={256}
				value={ramMb}
				required
				onChange={(event) => setRamMb(Number(event.target.value))}
			/>
			<span className='used'>{formatNumber(allocation.ram_mb)} used</span>

			<label htmlFor={`quota-cpu-${userId}`}>CPU (cores)</label>
			<input
				id={`quota-cpu-${userId}`}
				type='number'
				min={0}
				step={0.5}
				value={cpu}
				required
				onChange={(event) => setCpu(Number(event.target.value))}
			/>
			<span className='used'>{formatNumber(allocation.cpu)} used</span>

			<label htmlFor={`quota-disk-${userId}`}>Disk (GB)</label>
			<input
				id={`quota-disk-${userId}`}
				type='number'
				min={0}
				step={1}
				value={diskGb}
				required
				onChange={(event) => setDiskGb(Number(event.target.value))}
			/>
			<span className='used'>{formatNumber(allocation.disk_gb)} used</span>

			<button type='submit' disabled={!dirty || saving}>
				{saving ? "Saving…" : "Save quota"}
			</button>

			{/* Spelled out because a 0 in these fields means the opposite of what
			    it looks like: quota.py skips the check entirely at 0, so it is
			    "no ceiling", not "no allowance". */}
			<p className='hint'>0 = unlimited. Quotas cap the total across every assigned container.</p>

			{belowAllocation && (
				<p className='hint warn' role='status'>
					Below what this user already holds. Their containers keep running, but new assignments will be refused.
				</p>
			)}

			{/* role="alert" so a rejection is announced rather than just drawn;
			    the confirmation is role="status" because it is not an
			    interruption. The rejection carries the server's own wording. */}
			{error && (
				<p className='error' role='alert'>
					{error}
				</p>
			)}
			{saved && !dirty && (
				<p className='success' role='status'>
					Quota saved.
				</p>
			)}
		</form>
	);
}
