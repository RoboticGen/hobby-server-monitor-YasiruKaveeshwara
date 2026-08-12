/**
 * Live metric tile for a single container.
 *
 * Polls the backend for the newest sample and re-renders in place. One tile
 * per container is mounted by the dashboard, each polling independently.
 */
import { useEffect, useRef, useState } from "react";
import { apiFetch, ApiError } from "../lib/api";

/**
 * Poll interval, in milliseconds.
 *
 * Deliberately matches the collector's COLLECTOR_INTERVAL_SECONDS (10s):
 * the collector is the only thing that writes samples, so polling faster
 * would re-fetch a value that provably has not changed yet, and polling
 * slower would leave the tile showing stale numbers for no benefit.
 *
 * Polling at all is only cheap because of decision 7.5: this endpoint reads
 * the newest point out of TinyFlux and NEVER calls LXD. So the cost of this
 * interval is one small DB read per tile — it does not add load to the LXD
 * daemon, and it does not scale with how many dashboard tabs are open.
 */
const POLL_INTERVAL_MS = 10_000;

/**
 * One metric sample as stored by the collector.
 *
 * Field names mirror backend/lxd/client.py `get_container_state()` exactly,
 * since those keys are written to TinyFlux verbatim and served unmodified.
 */
export interface MetricPoint {
	time: string;
	cpu_usage_ns: number;
	ram_used_mb: number;
	ram_peak_mb: number;
	disk_used_mb: number;
	net_rx_bytes: number;
	net_tx_bytes: number;
	pid_count: number;
}

/** Response body of GET /api/metrics/latest?container=<id>. */
interface LatestMetricsResponse {
	container_id: string;
	point: MetricPoint | null;
}

export interface MetricTileProps {
	/** Container UUID — the value the metrics endpoint keys on. */
	containerId: string;
	/** Human-readable container name, for the tile heading. */
	name: string;
	/** Live LXD status ("Running", "Stopped", "Unknown") from GET /api/containers. */
	state: string;
	/** Current OS/image description from GET /api/containers. */
	osImage: string;
	/** IPv4 addresses reported by LXD for this container. */
	ipAddresses: string[];
	/** Creation timestamp from LXD, used to display uptime. */
	createdAt: string;
	/** Live process count from the latest LXD state. */
	processCount: number;
	/** Allocated RAM in MB (DB column limit_ram_mb). */
	ramAllocatedMb: number;
	/** Allocated disk in GB (DB column limit_disk_gb). */
	diskAllocatedGb: number;
	/**
	 * Newest sample already known at render time, if any.
	 *
	 * The dashboard passes this so the tile paints real numbers on first
	 * paint instead of flashing placeholders for a full poll interval.
	 */
	initialPoint?: MetricPoint | null;
}

/** Render a byte count as B/KB/MB/GB. Network counters span a huge range. */
function formatBytes(bytes: number): string {
	if (bytes < 1024) return `${Math.round(bytes)} B`;
	if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
	if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
	return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

/**
 * Convert two successive samples into a CPU percentage.
 *
 * The collector stores LXD's raw `cpu_usage_ns` — a counter of CPU-nanoseconds
 * consumed since the container started, NOT a percentage. A single sample is
 * therefore meaningless on its own; the percentage only exists as a rate
 * between two samples: busy-time elapsed divided by wall-time elapsed.
 *
 * The result is "percent of one core", so a container using two cores flat out
 * reads 200%. It is deliberately not divided by the allocated core count,
 * because that would silently rescale the number LXD reports.
 *
 * Returns null when no rate can be computed yet (first sample, or two samples
 * carrying the same timestamp), so the caller can show a placeholder rather
 * than a fabricated zero.
 */
function computeCpuPercent(previous: MetricPoint, current: MetricPoint): number | null {
	const elapsedMs = Date.parse(current.time) - Date.parse(previous.time);
	if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return null;

	const busyNs = current.cpu_usage_ns - previous.cpu_usage_ns;
	// A negative delta means the counter reset (container restarted), so the
	// rate is not meaningful across that boundary.
	if (busyNs < 0) return null;

	return (busyNs / (elapsedMs * 1_000_000)) * 100;
}

function computeRate(
	previous: MetricPoint,
	current: MetricPoint,
	field: "net_rx_bytes" | "net_tx_bytes",
): number | null {
	const elapsedMs = Date.parse(current.time) - Date.parse(previous.time);
	if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return null;

	const delta = current[field] - previous[field];
	if (!Number.isFinite(delta) || delta < 0) return null;

	return delta / (elapsedMs / 1000);
}

function formatPercent(part: number, whole: number): string {
	if (!Number.isFinite(whole) || whole <= 0) return "unlimited";
	return `${((part / whole) * 100).toFixed(1)}%`;
}

function formatStateLabel(state: string): string {
	if (state === "Unknown") return "Error";
	return state;
}

function formatUptime(createdAt: string): string {
	const elapsedMs = Date.now() - Date.parse(createdAt);
	if (!Number.isFinite(elapsedMs) || elapsedMs < 0) return "Unknown";

	const seconds = Math.floor(elapsedMs / 1000);
	const minutes = Math.floor(seconds / 60);
	const hours = Math.floor(minutes / 60);
	const days = Math.floor(hours / 24);

	if (days > 0) return `${days}d ${hours % 24}h`;
	if (hours > 0) return `${hours}h ${minutes % 60}m`;
	if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
	return `${seconds}s`;
}

export default function MetricTile({
	containerId,
	name,
	state,
	osImage,
	ipAddresses,
	createdAt,
	processCount,
	ramAllocatedMb,
	diskAllocatedGb,
	initialPoint = null,
}: MetricTileProps) {
	const [point, setPoint] = useState<MetricPoint | null>(initialPoint);
	const [cpuPercent, setCpuPercent] = useState<number | null>(null);
	const [rxRate, setRxRate] = useState<number | null>(null);
	const [txRate, setTxRate] = useState<number | null>(null);
	const [error, setError] = useState<string | null>(null);

	// Held in a ref, not state: the previous sample is an input to the next
	// percentage calculation, not something the UI renders. Putting it in state
	// would trigger an extra render on every poll for no visible change.
	const previousPoint = useRef<MetricPoint | null>(initialPoint);

	useEffect(() => {
		// Guards against a late response from a poll that was already in flight
		// when this tile unmounted (or when containerId changed) writing state
		// into a dead component.
		let cancelled = false;

		async function poll(): Promise<void> {
			try {
				const data = await apiFetch<LatestMetricsResponse>(
					`/api/metrics/latest?container=${encodeURIComponent(containerId)}`,
				);
				if (cancelled) return;

				// `point: null` is a legitimate answer, not an error: the container
				// exists but the collector has not written a sample for it yet.
				if (data.point === null) {
					setError(null);
					return;
				}

				const nextCpu = previousPoint.current ? computeCpuPercent(previousPoint.current, data.point) : null;
				const nextRxRate =
					previousPoint.current ? computeRate(previousPoint.current, data.point, "net_rx_bytes") : null;
				const nextTxRate =
					previousPoint.current ? computeRate(previousPoint.current, data.point, "net_tx_bytes") : null;
				// Keep the last computed rate when a fresh one is unavailable, so a
				// single counter reset does not blank an otherwise-live reading.
				if (nextCpu !== null) setCpuPercent(nextCpu);
				if (nextRxRate !== null) setRxRate(nextRxRate);
				if (nextTxRate !== null) setTxRate(nextTxRate);

				previousPoint.current = data.point;
				setPoint(data.point);
				setError(null);
			} catch (err) {
				if (cancelled) return;
				setError(err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach the server.");
			}
		}

		// Poll once immediately so a newly mounted tile does not sit empty for a
		// full interval, then settle into the fixed cadence.
		void poll();
		const timerId = setInterval(poll, POLL_INTERVAL_MS);

		// Clearing the interval on unmount is what stops a dashboard from leaking
		// a live timer per tile every time the page re-renders or navigates.
		return () => {
			cancelled = true;
			clearInterval(timerId);
		};
	}, [containerId]);

	return (
		<article className='tile'>
			<header>
				<h3>{name}</h3>
				<span className='state'>{formatStateLabel(state)}</span>
			</header>

			<p className='meta'>
				{osImage || "Unknown image"}
				{" • "}
				{ipAddresses.length > 0 ? ipAddresses[0] : "No IPv4"}
				{" • "}
				Up{" "}
				{state === "Running" ?
					formatUptime(createdAt)
				: state === "Frozen" ?
					"Frozen"
				: state === "Stopped" ?
					"Stopped"
				:	"Unknown"}
				{" • "}
				{processCount.toFixed(0)} procs
			</p>

			{error && (
				// The last known numbers stay on screen beneath this notice rather
				// than being cleared: stale data plus a warning is more useful to an
				// operator than an empty tile.
				<p className='error' role='status'>
					Live update failed ({error})
				</p>
			)}

			{point === null ?
				<p className='empty'>No metrics collected yet.</p>
			:	<dl>
					<dt>CPU</dt>
					<dd>{cpuPercent === null ? "measuring…" : `${cpuPercent.toFixed(1)}%`}</dd>

					<dt>RAM</dt>
					<dd>
						{point.ram_used_mb.toFixed(0)} / {ramAllocatedMb || "unlimited"} MB (
						{formatPercent(point.ram_used_mb, ramAllocatedMb)})
					</dd>

					<dt>Disk</dt>
					<dd>
						{(point.disk_used_mb / 1024).toFixed(2)} / {diskAllocatedGb || "unlimited"} GB (
						{formatPercent(point.disk_used_mb / 1024, diskAllocatedGb)})
					</dd>

					<dt>Network</dt>
					<dd>
						↓ {formatBytes(point.net_rx_bytes)} ({rxRate === null ? "n/a" : `${formatBytes(rxRate)}/s`}) ↑{" "}
						{formatBytes(point.net_tx_bytes)} ({txRate === null ? "n/a" : `${formatBytes(txRate)}/s`})
					</dd>
				</dl>
			}
		</article>
	);
}
