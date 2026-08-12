/**
 * Container creation form (admin only).
 *
 * Mounted on the admin dashboard. Collects a name, image, and resource
 * limits, then POSTs them to /api/containers and reports the server's own
 * verdict verbatim.
 */
import { useEffect, useState, type SyntheticEvent } from "react";
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

/** Response body of GET /api/lxd/options (backend/resources/containers.py). */
interface LxdOptionsResponse {
	images: { alias: string; description: string }[];
	networks: { name: string; type: string; managed: boolean }[];
	storage_pools: { name: string; driver: string }[];
	/** True when the lists are empty because LXD could not be reached. */
	stale: boolean;
	lxd_error: string | null;
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
 * PROJECT-PLAN 5.2 wants this list fetched from LXD at runtime; the backend
 * now serves it via GET /api/lxd/options, but that endpoint only sees images
 * already cached locally on the host — remote aliases like 'ubuntu:22.04'
 * resolve through the CLI's remotes, which the LXD HTTP API does not expose.
 * A fresh host returns an empty cache, so the dropdown would be empty even
 * though thousands of images are installable. The suggestions below fill
 * that gap; the live list, when non-empty, is appended to them.
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
	const [useCustomImage, setUseCustomImage] = useState<boolean>(false);
	const [ramMb, setRamMb] = useState<number>(512);
	const [cpu, setCpu] = useState<number>(1);
	const [diskGb, setDiskGb] = useState<number>(10);
	// Placement and lifecycle options. Defaults match the backend's: blank
	// placement inherits the default profile, and both toggles are off.
	const [network, setNetwork] = useState<string>("");
	const [storagePool, setStoragePool] = useState<string>("");
	const [ephemeral, setEphemeral] = useState<boolean>(false);
	const [autostart, setAutostart] = useState<boolean>(false);
	const [description, setDescription] = useState<string>("");

	// --- Request lifecycle ---
	const [accounting, setAccounting] = useState<AccountingResponse | null>(null);
	const [options, setOptions] = useState<LxdOptionsResponse | null>(null);
	const [submitting, setSubmitting] = useState<boolean>(false);
	const [error, setError] = useState<string | null>(null);
	const [success, setSuccess] = useState<string | null>(null);

	// Show LXD error messages returned by GET /api/lxd/options so admins
	// understand why the image list might be empty on this host.
	const lxdError = options?.lxd_error ?? null;

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
	 * Load the host's images, networks, and storage pools once on mount.
	 *
	 * Separate from the capacity effect and NOT keyed on `reloadKey`: pools
	 * and networks are host configuration that creating a container does not
	 * change, so re-fetching them after every create would be a round trip
	 * that can only return the same answer.
	 */
	useEffect(() => {
		let cancelled = false;

		async function loadOptions(): Promise<void> {
			try {
				const data = await apiFetch<LxdOptionsResponse>("/api/lxd/options");
				if (!cancelled) setOptions(data);
			} catch {
				// Same reasoning as capacity: these lists only populate
				// dropdowns, and every field they feed has a valid blank
				// default, so a failure here degrades the form rather than
				// breaking it.
				if (!cancelled) setOptions(null);
			}
		}

		void loadOptions();
		return () => {
			cancelled = true;
		};
	}, []);

	/**
	 * Submit the form.
	 *
	 * Client-side validation is limited to "is there a name at all". The LXD
	 * naming rules are not re-implemented here: the backend owns that regex and
	 * returns the exact rule as prose, so duplicating it would create a second
	 * copy to keep in sync and risk rejecting names the server would accept.
	 */
	async function handleSubmit(event: SyntheticEvent<HTMLFormElement>) {
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
					// Sent unconditionally, including when blank or false: the
					// backend treats "" as "inherit the default profile" and
					// false as off, so these are the explicit form of the
					// defaults rather than a request to change anything.
					network,
					storage_pool: storagePool,
					ephemeral,
					autostart,
					description,
				}),
			});

			setSuccess(`Created ${created.name} (${created.image}).`);
			// Name and description are cleared because they are unique to the
			// container just made; the limits and placement stay put, since
			// creating a batch of similar containers is the common case.
			setName("");
			setDescription("");
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
			{/* If the host reports cached images, present them as a dropdown so
					admins can see which real images are available. A "Custom..."
					option preserves the previous free-text behaviour for remote aliases. */}
			{options && options.images && options.images.length > 0 ?
				<>
					<select
						id='container-image-select'
						name='image_select'
						value={
							options.images.some((e) => e.alias === image) ? image
							: useCustomImage ?
								"__custom__"
							:	""
						}
						required
						onChange={(event) => {
							const v = event.target.value;
							if (v === "__custom__") {
								setUseCustomImage(true);
								setImage("");
							} else {
								setUseCustomImage(false);
								setImage(v);
							}
						}}>
						<option value=''>Choose an image…</option>
						{options.images.map((entry) => (
							<option key={entry.alias} value={entry.alias}>
								{entry.alias}
								{entry.description ? ` — ${entry.description}` : ""}
							</option>
						))}
						<option value='__custom__'>Custom alias…</option>
					</select>
					{useCustomImage && (
						<input
							id='container-image'
							name='image'
							value={image}
							required
							onChange={(event) => setImage(event.target.value)}
							placeholder='ubuntu:22.04 or myremote:myimage'
						/>
					)}
				</>
			:	<>
					<input
						id='container-image'
						name='image'
						list='image-suggestions'
						value={image}
						required
						onChange={(event) => setImage(event.target.value)}
					/>
					<datalist id='image-suggestions'>
						{(options?.images ?? []).map((entry) => (
							<option key={entry.alias} value={entry.alias} />
						))}
						{IMAGE_SUGGESTIONS.filter((alias) => !(options?.images ?? []).some((entry) => entry.alias === alias)).map(
							(alias) => (
								<option key={alias} value={alias} />
							),
						)}
					</datalist>
				</>
			}

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

			{/* Both dropdowns lead with a blank "default" option rather than
			    preselecting the first pool or network. Blank means "inherit
			    the default profile", which is what LXD does on its own — so
			    the default choice changes nothing, and any other choice is a
			    deliberate override. */}
			<label htmlFor='container-network'>Network</label>
			<select
				id='container-network'
				name='network'
				value={network}
				onChange={(event) => setNetwork(event.target.value)}>
				<option value=''>Default profile</option>
				{(options?.networks ?? []).map((entry) => (
					<option key={entry.name} value={entry.name}>
						{entry.name}
						{entry.type ? ` (${entry.type})` : ""}
					</option>
				))}
			</select>

			<label htmlFor='container-pool'>Storage pool</label>
			<select
				id='container-pool'
				name='storage_pool'
				value={storagePool}
				onChange={(event) => setStoragePool(event.target.value)}>
				<option value=''>Default profile</option>
				{(options?.storage_pools ?? []).map((entry) => (
					<option key={entry.name} value={entry.name}>
						{entry.name}
						{entry.driver ? ` (${entry.driver})` : ""}
					</option>
				))}
			</select>

			{/* Says why the two dropdowns above are empty. Without this an
			    unreachable LXD looks like a host with no networks or pools. */}
			{(options === null || options.stale) && (
				<p className='hint' role='status'>
					Could not read the host's networks and storage pools, so only the default profile is offered.
				</p>
			)}

			{/* Surface LXD-level errors (e.g., socket not found) so an admin can
				see why the dropdowns are empty instead of guessing. */}
			{lxdError && (
				<p className='hint error' role='status'>
					Could not read host images: {lxdError}
				</p>
			)}

			{/* When LXD was reachable but returned no cached images, tell the
				admin so they can import an image on the host (e.g. `lxc image copy`). */}
			{!options?.stale && Array.isArray(options?.images) && options.images.length === 0 && (
				<p className='hint' role='status'>
					No locally cached images found on this host. Import an image on the host (for example: `lxc image copy
					images:ubuntu/22.04 local: --alias ubuntu:22.04`) and reload this page.
				</p>
			)}

			<label htmlFor='container-description'>Description (optional)</label>
			<input
				id='container-description'
				name='description'
				value={description}
				onChange={(event) => setDescription(event.target.value)}
				placeholder='What this container is for'
			/>

			{/* Checkboxes wrap their label so the text is part of the hit
			    target, which a separate <label htmlFor> would not give. */}
			<label className='toggle'>
				<input
					type='checkbox'
					name='autostart'
					checked={autostart}
					onChange={(event) => setAutostart(event.target.checked)}
				/>
				Start automatically when the host boots
			</label>

			<label className='toggle'>
				<input
					type='checkbox'
					name='ephemeral'
					checked={ephemeral}
					onChange={(event) => setEphemeral(event.target.checked)}
				/>
				Ephemeral
			</label>
			{/* Spelled out because "ephemeral" understates it: this deletes the
			    container on its first stop, and there is no undo. */}
			<p className='hint'>Deletes itself permanently the first time it stops.</p>

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
