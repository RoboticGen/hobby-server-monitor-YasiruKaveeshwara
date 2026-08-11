/**
 * Container creation form (admin only).
 *
 * Mounted on the admin dashboard. Collects a name, image, and resource
 * limits, then POSTs them to /api/containers and reports the server's own
 * verdict verbatim.
 */
import { useEffect, useState, type FormEvent } from "react";
import { apiFetch, ApiError } from "../lib/api";

/** `host` block of GET /api/accounting. Null when LXD is unreachable. */
interface HostResources {
	cpu_cores: number;
	ram_total_mb: number;
	ram_used_mb: number;
	disk_total_gb: number;
	disk_used_gb: number;
}

/** `allocated` block of GET /api/accounting — summed from SQLite, so always current. */
interface AllocatedTotals {
	ram_mb: number;
	cpu: number;
	disk_gb: number;
	container_count: number;
}

/** Response body of GET /api/accounting (backend/resources/accounting.py). */
interface AccountingResponse {
	host: HostResources | null;
	allocated: AllocatedTotals;
	stale: boolean;
	host_error: string | null;
}

/** Response body of POST /api/containers on success (201). */
interface CreatedContainer {
	id: string;
	name: string;
	image: string;
	limits: { ram_mb: number; cpu: number; disk_gb: number };
}

/** Upper bounds the sliders allow, derived from capacity minus allocation. */
interface Headroom {
	ramMb: number;
	cpu: number;
	diskGb: number;
}

export interface CreateContainerFormProps {
	/**
	 * Called after a container is created successfully, so the dashboard can
	 * pull a fresh container list. Optional because the form is useful (and
	 * testable) without a parent that cares.
	 */
	onCreated?: () => void;
}

/**
 * Ceilings used ONLY when LXD is unreachable and real capacity is unknown.
 *
 * These are not "the limits" — they exist so the form stays usable during an
 * LXD outage instead of collapsing to a max of zero. The server re-validates
 * every limit against live capacity on submit (decision: server is the source
 * of truth), so a value accepted here can still be refused there, and that
 * refusal is what the admin sees.
 */
const FALLBACK_MAX_RAM_MB = 4096;
const FALLBACK_MAX_CPU = 8;
const FALLBACK_MAX_DISK_GB = 100;

/** Slider granularity. RAM in 256MB steps keeps the control usable at 64GB. */
const RAM_STEP_MB = 256;

/**
 * Image aliases offered as autocomplete hints.
 *
 * A plain suggestion list, deliberately NOT presented as the set of valid
 * images: the field stays free-text so any alias LXD accepts can be typed.
 * PROJECT-PLAN 5.2 wants this list fetched from LXD at runtime, which needs a
 * backend endpoint that does not exist yet — flagged in the step summary
 * rather than faked with a hardcoded dropdown that would look authoritative.
 */
const IMAGE_SUGGESTIONS = ["ubuntu:22.04", "ubuntu:24.04", "debian:12", "alpine:3.20"];

/**
 * Compute how much of the host is still unallocated.
 *
 * WHY THE BOUNDS COME FROM A LIVE BACKEND CALL AND NOT A HARDCODED CONSTANT:
 * a constant would be a second, silently diverging copy of the truth. The
 * host's real capacity is whatever LXD reports on this machine right now, and
 * the amount already promised to containers changes every time anyone creates
 * or deletes one. A hardcoded max would either sit below real capacity —
 * making the form refuse containers the host could actually run — or above
 * it, letting an admin drag a slider to a number the server is guaranteed to
 * reject. Deriving the ceiling from capacity minus allocation means the
 * control can only offer what is genuinely available.
 *
 * Note this subtracts *allocated* rather than *used*: a stopped container
 * with a 2GB limit still holds that 2GB against the host, so allocation is
 * the figure that decides whether a new container fits.
 */
function computeHeadroom(accounting: AccountingResponse | null): Headroom {
	if (!accounting?.host) {
		return {
			ramMb: FALLBACK_MAX_RAM_MB,
			cpu: FALLBACK_MAX_CPU,
			diskGb: FALLBACK_MAX_DISK_GB,
		};
	}

	const { host, allocated } = accounting;
	// Clamped at zero: a host that is already over-allocated would otherwise
	// produce a negative max, which renders as a broken slider.
	return {
		ramMb: Math.max(0, Math.floor(host.ram_total_mb - allocated.ram_mb)),
		cpu: Math.max(0, Math.floor(host.cpu_cores - allocated.cpu)),
		diskGb: Math.max(0, Math.floor(host.disk_total_gb - allocated.disk_gb)),
	};
}

/** Format an MB figure as GB for display once it passes 1024MB. */
function formatRam(mb: number): string {
	return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;
}

export default function CreateContainerForm({ onCreated }: CreateContainerFormProps) {
	// --- Form fields ---
	const [name, setName] = useState<string>("");
	const [image, setImage] = useState<string>(IMAGE_SUGGESTIONS[0]);
	const [ramMb, setRamMb] = useState<number>(512);
	const [cpu, setCpu] = useState<number>(1);
	const [diskGb, setDiskGb] = useState<number>(10);

	// --- Request lifecycle ---
	const [accounting, setAccounting] = useState<AccountingResponse | null>(null);
	const [submitting, setSubmitting] = useState<boolean>(false);
	const [error, setError] = useState<string | null>(null);
	const [success, setSuccess] = useState<string | null>(null);

	const headroom = computeHeadroom(accounting);

	/**
	 * Load host capacity once on mount, and again after each create.
	 *
	 * `reloadKey` is bumped by a successful submit rather than the accounting
	 * response being patched by hand: the backend is the only thing that knows
	 * the new allocation totals, so re-asking it is both simpler and correct,
	 * where local arithmetic would drift from the server's view.
	 */
	const [reloadKey, setReloadKey] = useState<number>(0);

	useEffect(() => {
		// Guards a late response from writing state into an unmounted form.
		let cancelled = false;

		async function loadCapacity(): Promise<void> {
			try {
				const data = await apiFetch<AccountingResponse>("/api/accounting");
				if (!cancelled) setAccounting(data);
			} catch {
				// Swallowed deliberately. Failing to read capacity must not block
				// creation: the form falls back to the ceilings above and the server
				// still enforces the real limits. Surfacing this as a form error
				// would imply the admin did something wrong.
				if (!cancelled) setAccounting(null);
			}
		}

		void loadCapacity();
		return () => {
			cancelled = true;
		};
	}, [reloadKey]);

	/**
	 * Submit the form.
	 *
	 * Client-side validation is limited to "is there a name at all". The LXD
	 * naming rules are not re-implemented here: the backend owns that regex and
	 * returns the exact rule as prose, so duplicating it would create a second
	 * copy to keep in sync and risk rejecting names the server would accept.
	 */
	async function handleSubmit(event: FormEvent<HTMLFormElement>) {
		event.preventDefault();
		setSubmitting(true);
		setError(null);
		setSuccess(null);

		try {
			const created = await apiFetch<CreatedContainer>("/api/containers", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					name,
					image,
					limits: { ram_mb: ramMb, cpu, disk_gb: diskGb },
				}),
			});

			setSuccess(`Created ${created.name} (${created.image}).`);
			setName("");
			setReloadKey((key) => key + 1);
			onCreated?.();
		} catch (err) {
			// The server's own message is shown verbatim — its validation text
			// names the actual rule that was broken (the container-name regex, a
			// quota ceiling, a duplicate name), which a generic "something went
			// wrong" would throw away.
			setError(err instanceof ApiError ? err.message : "Could not reach the server to create the container.");
		} finally {
			setSubmitting(false);
		}
	}

	return (
		<form className='create-form' onSubmit={handleSubmit}>
			<h2>Create container</h2>

			{/* Says plainly that the ceilings below are guesses, so an admin does
          not read a fallback maximum as real host capacity. */}
			{accounting === null && (
				<p className='hint' role='status'>
					Host capacity unavailable, showing default limits. The server still enforces the real ones.
				</p>
			)}
			{accounting?.stale && (
				<p className='hint' role='status'>
					LXD is unreachable, so host capacity is unknown. Allocation figures are still accurate.
				</p>
			)}

			<label htmlFor='container-name'>Name</label>
			<input
				id='container-name'
				name='name'
				value={name}
				required
				onChange={(event) => setName(event.target.value)}
				placeholder='web-server-01'
			/>
			{/* A hint, not a validator: the server owns the rule and its rejection
          message is what the admin ultimately sees. */}
			<p className='hint'>Lowercase letters, digits and hyphens. Must start with a letter.</p>

			<label htmlFor='container-image'>Image</label>
			<input
				id='container-image'
				name='image'
				list='image-suggestions'
				value={image}
				required
				onChange={(event) => setImage(event.target.value)}
			/>
			<datalist id='image-suggestions'>
				{IMAGE_SUGGESTIONS.map((alias) => (
					<option key={alias} value={alias} />
				))}
			</datalist>

			<label htmlFor='container-ram'>
				RAM: {formatRam(ramMb)} of {formatRam(headroom.ramMb)} available
			</label>
			<input
				id='container-ram'
				name='ram'
				type='range'
				min={RAM_STEP_MB}
				max={Math.max(RAM_STEP_MB, headroom.ramMb)}
				step={RAM_STEP_MB}
				value={ramMb}
				onChange={(event) => setRamMb(Number(event.target.value))}
			/>

			{/* A stepper rather than a slider: core counts are small integers where
          an exact value matters, and dragging for "2" is worse than typing it. */}
			<label htmlFor='container-cpu'>CPU cores (max {headroom.cpu || FALLBACK_MAX_CPU})</label>
			<input
				id='container-cpu'
				name='cpu'
				type='number'
				min={1}
				max={Math.max(1, headroom.cpu || FALLBACK_MAX_CPU)}
				step={1}
				value={cpu}
				onChange={(event) => setCpu(Number(event.target.value))}
			/>

			<label htmlFor='container-disk'>
				Disk: {diskGb} GB of {headroom.diskGb} GB available
			</label>
			<input
				id='container-disk'
				name='disk'
				type='range'
				min={1}
				max={Math.max(1, headroom.diskGb)}
				step={1}
				value={diskGb}
				onChange={(event) => setDiskGb(Number(event.target.value))}
			/>

			<button type='submit' disabled={submitting}>
				{submitting ? "Creating…" : "Create container"}
			</button>

			{/* role="alert" so the failure is announced, not just drawn. The
          server's wording is passed through untouched. */}
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
