/**
 * Container creation form (admin only).
 *
 * Mounted on the admin dashboard. Collects a name, image, and resource
 * limits, then POSTs them to /api/containers and reports the server's own
 * verdict verbatim.
 */
import { useEffect, useState, type SyntheticEvent } from "react";
import { apiFetch, ApiError } from "../lib/api";

interface HostResources {
	cpu_cores: number;
	ram_total_mb: number;
	ram_used_mb: number;
	disk_total_gb: number;
	disk_used_gb: number;
}

interface AllocatedTotals {
	ram_mb: number;
	cpu: number;
	disk_gb: number;
	container_count: number;
}

interface UserQuota {
	ram_mb: number;
	cpu: number;
	disk_gb: number;
}

interface UserAllocation {
	ram_mb: number;
	cpu: number;
	disk_gb: number;
}

interface UserAccountingRow {
	id: string;
	email: string;
	role: string;
	status: string;
	allocation: UserAllocation;
	quota: UserQuota;
	container_count: number;
}

interface AccountingResponse {
	host: HostResources | null;
	allocated: AllocatedTotals;
	users: UserAccountingRow[];
	stale: boolean;
	host_error: string | null;
}

interface CreatedContainer {
	id: string;
	name: string;
	image: string;
	limits: { ram_mb: number; cpu: number; disk_gb: number };
}

interface LxdOptionsResponse {
	images: { alias: string; description: string }[];
	networks: { name: string; type: string; managed: boolean }[];
	storage_pools: {
		name: string;
		driver: string;
		total_gb: number;
		used_gb: number;
		available_gb: number;
	}[];
	stale: boolean;
	lxd_error: string | null;
}

interface Headroom {
	ramMb: number;
	cpu: number;
	diskGb: number;
}

export interface CreateContainerFormProps {
	onCreated?: () => void;
}

const FALLBACK_MAX_RAM_MB = 4096;
const FALLBACK_MAX_CPU = 8;
const FALLBACK_MAX_DISK_GB = 100;
const RAM_STEP_MB = 256;

function computeHostHeadroom(accounting: AccountingResponse | null): Headroom {
	if (!accounting?.host) {
		return {
			ramMb: FALLBACK_MAX_RAM_MB,
			cpu: FALLBACK_MAX_CPU,
			diskGb: FALLBACK_MAX_DISK_GB,
		};
	}

	const { host, allocated } = accounting;
	return {
		ramMb: Math.max(0, Math.floor(host.ram_total_mb - allocated.ram_mb)),
		cpu: Math.max(0, Math.floor(host.cpu_cores - allocated.cpu)),
		diskGb: Math.max(0, Math.floor(host.disk_total_gb - allocated.disk_gb)),
	};
}

function computeRemainingQuota(user: UserAccountingRow): Headroom {
	return {
		ramMb: user.quota.ram_mb === 0 ? FALLBACK_MAX_RAM_MB : Math.max(0, user.quota.ram_mb - user.allocation.ram_mb),
		cpu: user.quota.cpu === 0 ? FALLBACK_MAX_CPU : Math.max(0, user.quota.cpu - user.allocation.cpu),
		diskGb: user.quota.disk_gb === 0 ? FALLBACK_MAX_DISK_GB : Math.max(0, user.quota.disk_gb - user.allocation.disk_gb),
	};
}

function computeEffectiveHeadroom(
	accounting: AccountingResponse | null,
	selectedAssigneeId: string,
	hostHeadroom: Headroom,
	selectedPoolDiskGb: number,
): Headroom {
	if (!accounting || !selectedAssigneeId) {
		return {
			ramMb: hostHeadroom.ramMb,
			cpu: hostHeadroom.cpu,
			diskGb: Math.min(hostHeadroom.diskGb, selectedPoolDiskGb),
		};
	}

	const assignee = accounting.users.find((user) => user.id === selectedAssigneeId);
	const quotaHeadroom = assignee ? computeRemainingQuota(assignee) : hostHeadroom;
	return {
		ramMb: Math.min(hostHeadroom.ramMb, quotaHeadroom.ramMb),
		cpu: Math.min(hostHeadroom.cpu, quotaHeadroom.cpu),
		diskGb: Math.min(hostHeadroom.diskGb, quotaHeadroom.diskGb, selectedPoolDiskGb),
	};
}

function formatRam(mb: number): string {
	return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;
}

export default function CreateContainerForm({ onCreated }: CreateContainerFormProps) {
	const [name, setName] = useState<string>("");
	const [image, setImage] = useState<string>("");
	const [assigneeId, setAssigneeId] = useState<string>("");
	const [ramMb, setRamMb] = useState<number>(512);
	const [cpu, setCpu] = useState<number>(1);
	const [diskGb, setDiskGb] = useState<number>(10);
	const [network, setNetwork] = useState<string>("");
	const [storagePool, setStoragePool] = useState<string>("");
	const [ephemeral, setEphemeral] = useState<boolean>(false);
	const [autostart, setAutostart] = useState<boolean>(false);
	const [description, setDescription] = useState<string>("");

	const [accounting, setAccounting] = useState<AccountingResponse | null>(null);
	const [options, setOptions] = useState<LxdOptionsResponse | null>(null);
	const [submitting, setSubmitting] = useState<boolean>(false);
	const [error, setError] = useState<string | null>(null);
	const [success, setSuccess] = useState<string | null>(null);
	const [reloadKey, setReloadKey] = useState<number>(0);

	const hostHeadroom = computeHostHeadroom(accounting);
	const selectedAssignee = accounting?.users.find((user) => user.id === assigneeId) ?? null;
	const selectedStoragePool = options?.storage_pools.find((pool) => pool.name === storagePool) ?? null;
	const selectedPoolDiskGb = selectedStoragePool?.available_gb ?? hostHeadroom.diskGb;
	const effectiveHeadroom = computeEffectiveHeadroom(accounting, assigneeId, hostHeadroom, selectedPoolDiskGb);
	const canCreate =
		effectiveHeadroom.ramMb >= RAM_STEP_MB && effectiveHeadroom.cpu >= 1 && effectiveHeadroom.diskGb >= 1;

	useEffect(() => {
		if (ramMb > effectiveHeadroom.ramMb) {
			setRamMb(
				Math.max(Math.min(ramMb, effectiveHeadroom.ramMb), effectiveHeadroom.ramMb >= RAM_STEP_MB ? RAM_STEP_MB : 0),
			);
		}
		if (cpu > effectiveHeadroom.cpu) {
			setCpu(Math.max(Math.min(cpu, effectiveHeadroom.cpu), effectiveHeadroom.cpu >= 1 ? 1 : 0));
		}
		if (diskGb > effectiveHeadroom.diskGb) {
			setDiskGb(Math.max(Math.min(diskGb, effectiveHeadroom.diskGb), effectiveHeadroom.diskGb >= 1 ? 1 : 0));
		}
	}, [effectiveHeadroom, ramMb, cpu, diskGb]);

	useEffect(() => {
		let cancelled = false;

		async function loadCapacity(): Promise<void> {
			try {
				const data = await apiFetch<AccountingResponse>("/api/accounting");
				if (!cancelled) setAccounting(data);
			} catch {
				if (!cancelled) setAccounting(null);
			}
		}

		void loadCapacity();
		return () => {
			cancelled = true;
		};
	}, [reloadKey]);

	useEffect(() => {
		let cancelled = false;

		async function loadOptions(): Promise<void> {
			try {
				const data = await apiFetch<LxdOptionsResponse>("/api/lxd/options");
				if (!cancelled) setOptions(data);
			} catch {
				if (!cancelled) setOptions(null);
			}
		}

		void loadOptions();
		return () => {
			cancelled = true;
		};
	}, []);

	async function handleSubmit(event: SyntheticEvent<HTMLFormElement>): Promise<void> {
		event.preventDefault();
		setSubmitting(true);
		setError(null);
		setSuccess(null);

		const body: Record<string, unknown> = {
			name: name.trim(),
			image: image.trim(),
			limits: {
				ram_mb: ramMb,
				cpu,
				disk_gb: diskGb,
			},
		};

		if (assigneeId) body.assign_to = assigneeId;
		if (network) body.network = network;
		if (storagePool) body.storage_pool = storagePool;
		if (ephemeral) body.ephemeral = true;
		if (autostart) body.autostart = true;
		if (description.trim()) body.description = description.trim();

		try {
			const created = await apiFetch<CreatedContainer>("/api/containers", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(body),
			});

			setSuccess(`Container '${created.name}' initialized and created successfully.`);
			setName("");
			setDescription("");
			setReloadKey((key) => key + 1);
			onCreated?.();
		} catch (err) {
			setError(err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach the server.");
		} finally {
			setSubmitting(false);
		}
	}

	return (
		<section className='create-container-section glass-card'>
			<div className='form-header'>
				<div className='header-icon'>
					<svg width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2'>
						<line x1='12' y1='5' x2='12' y2='19'></line>
						<line x1='5' y1='12' x2='19' y2='12'></line>
					</svg>
				</div>
				<div>
					<h2>Create New Container</h2>
					<p className='section-desc'>Deploy an isolated unprivileged LXD container with resource limits.</p>
				</div>
			</div>

			{error && <div className='alert alert-error'>{error}</div>}
			{success && <div className='alert alert-success'>{success}</div>}

			{!canCreate && (
				<div className='alert alert-warning'>
					Host capacity or target user quota is exhausted. Adjust existing containers to free resources.
				</div>
			)}

			<form className='creation-form' onSubmit={(e) => void handleSubmit(e)}>
				<div className='form-grid'>
					{/* Name Field */}
					<div className='form-group'>
						<label htmlFor='container-name'>Container Identifier</label>
						<input
							id='container-name'
							type='text'
							value={name}
							onChange={(e) => setName(e.target.value)}
							placeholder='e.g. web-worker-01'
							pattern='^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$'
							title='1-63 chars, lowercase letters, numbers, and hyphens. Must start with a letter.'
							required
							disabled={submitting}
						/>
						<span className='field-hint'>Lowercase letters, digits, hyphens only.</span>
					</div>

					{/* Image Selection */}
					<div className='form-group'>
						<label htmlFor='container-image'>OS Image</label>
						{options && options.images.length > 0 ?
							<select
								id='container-image'
								value={image}
								onChange={(e) => setImage(e.target.value)}
								required
								disabled={submitting}>
								<option value=''>Select cached image…</option>
								{options.images.map((img) => (
									<option key={img.alias} value={img.alias}>
										{img.alias} {img.description ? `(${img.description})` : ""}
									</option>
								))}
							</select>
						:	<input
								id='container-image'
								type='text'
								value={image}
								onChange={(e) => setImage(e.target.value)}
								placeholder='e.g. ubuntu:22.04'
								required
								disabled={submitting}
							/>
						}
						<span className='field-hint'>LXD image alias or remote image identifier.</span>
					</div>

					{/* Target User Pre-Assignment */}
					<div className='form-group'>
						<label htmlFor='container-assignee'>Initial Ownership</label>
						<select
							id='container-assignee'
							value={assigneeId}
							onChange={(e) => setAssigneeId(e.target.value)}
							disabled={submitting}>
							<option value=''>Unassigned (Admin management only)</option>
							{accounting?.users.map((user) => (
								<option key={user.id} value={user.id}>
									{user.email} ({user.role})
								</option>
							))}
						</select>
						<span className='field-hint'>Pre-assign container to a user during provisioning.</span>
					</div>

					{/* Optional Description */}
					<div className='form-group'>
						<label htmlFor='container-description'>Description (Optional)</label>
						<input
							id='container-description'
							type='text'
							value={description}
							onChange={(e) => setDescription(e.target.value)}
							placeholder='e.g. Production microservice node'
							disabled={submitting}
						/>
					</div>
				</div>

				{/* Resource Sliders Group */}
				<div className='sliders-panel'>
					<h3>Resource Allocation</h3>

					<div className='slider-box'>
						<div className='slider-header'>
							<label htmlFor='ram-slider'>RAM Allocation</label>
							<span className='slider-value mono'>{formatRam(ramMb)}</span>
						</div>
						<input
							id='ram-slider'
							type='range'
							min={RAM_STEP_MB}
							max={Math.max(RAM_STEP_MB, effectiveHeadroom.ramMb)}
							step={RAM_STEP_MB}
							value={ramMb}
							onChange={(e) => setRamMb(Number(e.target.value))}
							disabled={submitting || effectiveHeadroom.ramMb < RAM_STEP_MB}
						/>
						<div className='slider-footer'>
							<span>Min: 256 MB</span>
							<span>Max Headroom: {formatRam(effectiveHeadroom.ramMb)}</span>
						</div>
					</div>

					<div className='slider-box'>
						<div className='slider-header'>
							<label htmlFor='cpu-slider'>CPU Core Limits</label>
							<span className='slider-value mono'>{cpu} Core(s)</span>
						</div>
						<input
							id='cpu-slider'
							type='range'
							min={1}
							max={Math.max(1, effectiveHeadroom.cpu)}
							step={1}
							value={cpu}
							onChange={(e) => setCpu(Number(e.target.value))}
							disabled={submitting || effectiveHeadroom.cpu < 1}
						/>
						<div className='slider-footer'>
							<span>Min: 1 Core</span>
							<span>Max Headroom: {effectiveHeadroom.cpu} Cores</span>
						</div>
					</div>

					<div className='slider-box'>
						<div className='slider-header'>
							<label htmlFor='disk-slider'>Disk Allocation</label>
							<span className='slider-value mono'>{diskGb} GB</span>
						</div>
						<input
							id='disk-slider'
							type='range'
							min={1}
							max={Math.max(1, effectiveHeadroom.diskGb)}
							step={1}
							value={diskGb}
							onChange={(e) => setDiskGb(Number(e.target.value))}
							disabled={submitting || effectiveHeadroom.diskGb < 1}
						/>
						<div className='slider-footer'>
							<span>Min: 1 GB</span>
							<span>Max Headroom: {effectiveHeadroom.diskGb} GB</span>
						</div>
					</div>
				</div>

				{/* Advanced Configuration Accordion / Row */}
				<div className='advanced-grid'>
					<div className='form-group'>
						<label htmlFor='container-network'>Network Bridge</label>
						<select
							id='container-network'
							value={network}
							onChange={(e) => setNetwork(e.target.value)}
							disabled={submitting}>
							<option value=''>Default Host Profile</option>
							{options?.networks.map((net) => (
								<option key={net.name} value={net.name}>
									{net.name} ({net.type || "bridge"})
								</option>
							))}
						</select>
					</div>

					<div className='form-group'>
						<label htmlFor='container-storage-pool'>Storage Pool</label>
						<select
							id='container-storage-pool'
							value={storagePool}
							onChange={(e) => setStoragePool(e.target.value)}
							disabled={submitting}>
							<option value=''>Default Pool</option>
							{options?.storage_pools.map((pool) => (
								<option key={pool.name} value={pool.name}>
									{pool.name} ({pool.driver}) — {pool.available_gb} GB free
								</option>
							))}
						</select>
					</div>
				</div>

				{/* Toggles */}
				<div className='toggles-row'>
					<label className='toggle-label'>
						<input
							type='checkbox'
							checked={ephemeral}
							onChange={(e) => setEphemeral(e.target.checked)}
							disabled={submitting}
						/>
						<span>Ephemeral (destroy on stop)</span>
					</label>

					<label className='toggle-label'>
						<input
							type='checkbox'
							checked={autostart}
							onChange={(e) => setAutostart(e.target.checked)}
							disabled={submitting}
						/>
						<span>Autostart on system boot</span>
					</label>
				</div>

				<div className='form-actions'>
					<button type='submit' className='primary' disabled={submitting || !canCreate}>
						{submitting ? "Provisioning Container…" : "Provision & Launch Container"}
					</button>
				</div>
			</form>
		</section>
	);
}
