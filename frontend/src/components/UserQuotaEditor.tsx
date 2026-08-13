/**
 * Inline quota editor for one user.
 *
 * Renders the three quota fields alongside what the user currently holds, and
 * PATCHes /api/users/{id} when they change.
 */
import { useState, type SyntheticEvent } from "react";
import { apiFetch, ApiError } from "../lib/api";

export interface QuotaAllocation {
	ram_mb: number;
	cpu: number;
	disk_gb: number;
}

interface UpdatedUser {
	id: string;
	email: string;
	role: string;
	status: string;
	quota_ram_mb: number;
	quota_cpu: number;
	quota_disk_gb: number;
}

interface QuotaPatch {
	quota_ram_mb?: number;
	quota_cpu?: number;
	quota_disk_gb?: number;
}

interface QuotaValues {
	ramMb: number;
	cpu: number;
	diskGb: number;
}

export interface UserQuotaEditorProps {
	userId: string;
	quotaRamMb: number;
	quotaCpu: number;
	quotaDiskGb: number;
	allocation: QuotaAllocation;
	onSaved?: () => void;
}

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
	const [ramMb, setRamMb] = useState<number>(quotaRamMb);
	const [cpu, setCpu] = useState<number>(quotaCpu);
	const [diskGb, setDiskGb] = useState<number>(quotaDiskGb);

	const [baseline, setBaseline] = useState<QuotaValues>({
		ramMb: quotaRamMb,
		cpu: quotaCpu,
		diskGb: quotaDiskGb,
	});

	const [saving, setSaving] = useState<boolean>(false);
	const [error, setError] = useState<string | null>(null);
	const [saved, setSaved] = useState<boolean>(false);

	const dirty = ramMb !== baseline.ramMb || cpu !== baseline.cpu || diskGb !== baseline.diskGb;

	const ramWarning = ramMb > 0 && allocation.ram_mb > ramMb;
	const cpuWarning = cpu > 0 && allocation.cpu > cpu;
	const diskWarning = diskGb > 0 && allocation.disk_gb > diskGb;
	const hasWarning = ramWarning || cpuWarning || diskWarning;

	async function handleSubmit(event: SyntheticEvent<HTMLFormElement>): Promise<void> {
		event.preventDefault();
		setSaving(true);
		setError(null);
		setSaved(false);

		const patch: QuotaPatch = {};
		if (ramMb !== baseline.ramMb) patch.quota_ram_mb = ramMb;
		if (cpu !== baseline.cpu) patch.quota_cpu = cpu;
		if (diskGb !== baseline.diskGb) patch.quota_disk_gb = diskGb;

		if (Object.keys(patch).length === 0) {
			setSaving(false);
			return;
		}

		try {
			const updated = await apiFetch<UpdatedUser>(`/api/users/${encodeURIComponent(userId)}`, {
				method: "PATCH",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(patch),
			});

			setBaseline({
				ramMb: updated.quota_ram_mb,
				cpu: updated.quota_cpu,
				diskGb: updated.quota_disk_gb,
			});
			setSaved(true);
			onSaved?.();
		} catch (err) {
			setError(err instanceof ApiError ? err.message : "Could not reach the server to save quotas.");
		} finally {
			setSaving(false);
		}
	}

	return (
		<form className='quota-editor-panel' onSubmit={(e) => void handleSubmit(e)}>
			<h4>Resource Quota Allocation</h4>

			{error && <div className='alert alert-error'>{error}</div>}
			{saved && !dirty && <div className='alert alert-success'>Resource quotas updated successfully.</div>}

			{hasWarning && (
				<div className='alert alert-warning'>
					New quota is lower than current allocation ({formatNumber(allocation.ram_mb)}MB RAM,{" "}
					{formatNumber(allocation.cpu)} CPU, {formatNumber(allocation.disk_gb)}GB Disk). Existing containers will keep
					running, but creating new ones will be blocked.
				</div>
			)}

			<div className='quota-editor-grid'>
				<div className='form-group'>
					<label htmlFor={`quota-ram-${userId}`}>RAM Quota (MB)</label>
					<input
						id={`quota-ram-${userId}`}
						type='number'
						min={0}
						step={256}
						value={ramMb}
						onChange={(e) => {
							setRamMb(Number(e.target.value));
							setSaved(false);
						}}
						disabled={saving}
					/>
					<span className='field-hint'>Currently using {formatNumber(allocation.ram_mb)} MB</span>
				</div>

				<div className='form-group'>
					<label htmlFor={`quota-cpu-${userId}`}>CPU Cores Quota</label>
					<input
						id={`quota-cpu-${userId}`}
						type='number'
						min={0}
						step={0.5}
						value={cpu}
						onChange={(e) => {
							setCpu(Number(e.target.value));
							setSaved(false);
						}}
						disabled={saving}
					/>
					<span className='field-hint'>Currently using {formatNumber(allocation.cpu)} Cores</span>
				</div>

				<div className='form-group'>
					<label htmlFor={`quota-disk-${userId}`}>Disk Quota (GB)</label>
					<input
						id={`quota-disk-${userId}`}
						type='number'
						min={0}
						step={1}
						value={diskGb}
						onChange={(e) => {
							setDiskGb(Number(e.target.value));
							setSaved(false);
						}}
						disabled={saving}
					/>
					<span className='field-hint'>Currently using {formatNumber(allocation.disk_gb)} GB</span>
				</div>
			</div>

			<div className='editor-actions'>
				<button type='submit' className='primary' disabled={!dirty || saving}>
					{saving ? "Saving…" : "Apply Quota Limits"}
				</button>
			</div>
		</form>
	);
}
