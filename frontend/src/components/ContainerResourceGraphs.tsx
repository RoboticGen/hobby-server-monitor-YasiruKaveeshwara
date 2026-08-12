/**
 * ContainerResourceGraphs
 *
 * Renders live real-time resource charts (CPU, RAM, Disk, Network) for a
 * container.
 *
 * DEFAULT BEHAVIOUR — "Live" mode:
 *   - On mount, fetches the last 60 raw collector samples (~5 min) from
 *     /api/metrics/recent to pre-populate the charts immediately.
 *   - Then polls /api/metrics/latest every 5 seconds, appending each new
 *     sample to the end of the series so the graph scrolls forward in real
 *     time.
 *   - This means the user sees real data from the very first render,
 *     not the "no samples" placeholder that appeared when starting cold.
 *
 * BUNDLE MODES — "1h" / "24h" / "7d":
 *   - Fetches the pre-aggregated history from /api/containers/{id}/history.
 *   - The live 5-second poll is still running in the background so that
 *     switching back to "Live" gives an already-current chart.
 *
 * Why separate live vs bundle:
 *   - The bundle endpoint returns downsampled (5m or 1h average) points so
 *     it is not suitable as a live feed — the granularity is too coarse.
 *   - The live endpoint returns individual raw collector snapshots at
 *     collector_interval_seconds cadence (default 5 s).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, ApiError } from "../lib/api";
import HistoryChart, { type HistoryPoint } from "./HistoryChart";

// ── Types ──────────────────────────────────────────────────────────────────

type ViewMode = "live" | "1h" | "24h" | "7d";

/** One raw collector sample as written to TinyFlux by the collector. */
interface HistoryRow {
	time: string;
	cpu_usage_ns: number;
	ram_used_mb: number;
	disk_used_mb: number;
	net_rx_bytes: number;
	net_tx_bytes: number;
	pid_count: number;
}

/** Response shape of GET /api/metrics/recent */
interface RecentResponse {
	container_id: string;
	points: HistoryRow[];
	count: number;
}

/** Response shape of GET /api/metrics/latest */
interface LatestResponse {
	container_id: string;
	point: HistoryRow | null;
}

/** Response shape of GET /api/containers/{id}/history */
interface HistoryResponse {
	container_id: string;
	window: string;
	resolution: string;
	points: HistoryRow[];
}

interface ContainerResourceGraphsProps {
	containerId: string;
	ramAllocatedMb: number;
	cpuAllocated: number;
	diskAllocatedGb: number;
}

// ── Helpers ────────────────────────────────────────────────────────────────

function formatBytes(bytesPerSecond: number): string {
	if (bytesPerSecond < 1024) return `${Math.round(bytesPerSecond)} B/s`;
	if (bytesPerSecond < 1024 ** 2) return `${(bytesPerSecond / 1024).toFixed(1)} KB/s`;
	if (bytesPerSecond < 1024 ** 3) return `${(bytesPerSecond / 1024 ** 2).toFixed(1)} MB/s`;
	return `${(bytesPerSecond / 1024 ** 3).toFixed(2)} GB/s`;
}

function formatPercent(value: number): string {
	return `${value.toFixed(1)}%`;
}

/**
 * Derive CPU % from two consecutive samples.
 * cpu_usage_ns is a cumulative counter, so we need delta / elapsed.
 * Returns null when the rate cannot be computed (first point, or counter reset).
 */
function computeCpuPercent(previous: HistoryRow, current: HistoryRow): number | null {
	const elapsedMs = Date.parse(current.time) - Date.parse(previous.time);
	if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return null;

	const busyNs = current.cpu_usage_ns - previous.cpu_usage_ns;
	if (busyNs < 0) return null; // counter reset (container restarted)

	return (busyNs / (elapsedMs * 1_000_000)) * 100;
}

function computeRate(previous: HistoryRow, current: HistoryRow, field: "net_rx_bytes" | "net_tx_bytes"): number | null {
	const elapsedMs = Date.parse(current.time) - Date.parse(previous.time);
	if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return null;

	const delta = current[field] - previous[field];
	if (!Number.isFinite(delta) || delta < 0) return null;

	return delta / (elapsedMs / 1000);
}

/** Build a CPU % series from an array of raw points. Requires ≥2 points. */
function buildCpuSeries(points: HistoryRow[]): HistoryPoint[] {
	const series: HistoryPoint[] = [];
	for (let i = 1; i < points.length; i += 1) {
		const value = computeCpuPercent(points[i - 1], points[i]);
		if (value !== null) series.push({ time: points[i].time, value });
	}
	return series;
}

function buildValueSeries(
	points: HistoryRow[],
	selector: (row: HistoryRow) => number,
	transform: (value: number) => number = (v) => v,
): HistoryPoint[] {
	return points.map((p) => ({ time: p.time, value: transform(selector(p)) }));
}

function buildNetworkSeries(points: HistoryRow[]): HistoryPoint[] {
	const series: HistoryPoint[] = [];
	for (let i = 1; i < points.length; i += 1) {
		const rx = computeRate(points[i - 1], points[i], "net_rx_bytes");
		const tx = computeRate(points[i - 1], points[i], "net_tx_bytes");
		if (rx === null || tx === null) continue;
		series.push({ time: points[i].time, value: rx + tx });
	}
	return series;
}

// ── Component ──────────────────────────────────────────────────────────────

/**
 * Max points kept in the live rolling window.
 * 720 = 60 minutes at a 5-second collector interval.
 * Keeps memory bounded while still giving a full hour of live history.
 */
const LIVE_MAX_POINTS = 720;

/** Poll every 5 seconds in live mode — matches the collector's default interval. */
const LIVE_POLL_MS = 5_000;

/** Refresh bundle data every 30 s in bundle mode to catch new aggregations. */
const BUNDLE_REFRESH_MS = 30_000;

const VIEW_MODES: Array<{ value: ViewMode; label: string }> = [
	{ value: "live", label: "Live (5s)" },
	{ value: "1h", label: "1h bundle" },
	{ value: "24h", label: "24h bundle" },
	{ value: "7d", label: "7d bundle" },
];

export default function ContainerResourceGraphs({
	containerId,
	ramAllocatedMb,
	cpuAllocated,
	diskAllocatedGb,
}: ContainerResourceGraphsProps) {
	const [viewMode, setViewMode] = useState<ViewMode>("live");

	// Live graph state
	// livePoints is the rolling window of raw collector samples.
	// seeded tracks whether we have already fetched the initial recent batch.
	const [livePoints, setLivePoints] = useState<HistoryRow[]>([]);
	const [seeded, setSeeded] = useState(false);
	const [liveError, setLiveError] = useState<string | null>(null);
	const [liveStatus, setLiveStatus] = useState<string>("Loading live data…");

	// Bundle history state (used by 1h / 24h / 7d modes)
	const [bundleHistory, setBundleHistory] = useState<HistoryResponse | null>(null);
	const [bundleError, setBundleError] = useState<string | null>(null);
	const [bundleStatus, setBundleStatus] = useState<string>("");

	// Used to derive the latest current-value stats shown in card headers
	const latestLivePoint = livePoints.at(-1) ?? null;

	// ── Effect 1: Seed live graph with recent history on mount / container change ──
	useEffect(() => {
		let cancelled = false;

		async function seed(): Promise<void> {
			try {
				const data = await apiFetch<RecentResponse>(
					`/api/metrics/recent?container=${encodeURIComponent(containerId)}&limit=120`,
				);
				if (cancelled) return;

				if (data.points.length > 0) {
					// Replace any existing live points with the seeded batch.
					// Subsequent polls will append to this.
					setLivePoints(data.points);
					setLiveStatus(`Live — ${data.points.length} samples pre-loaded. Updating every 5s.`);
				} else {
					// No data in TinyFlux yet — the collector hasn't run a full
					// cycle for this container. The poll below will update once it does.
					setLiveStatus("Waiting for the first sample from the collector…");
				}
				setSeeded(true);
				setLiveError(null);
			} catch (err) {
				if (cancelled) return;
				setLiveError(err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach server.");
				setLiveStatus("Could not pre-load live data.");
				// Still mark seeded so the poll loop starts regardless.
				setSeeded(true);
			}
		}

		// Reset on container change
		setSeeded(false);
		setLivePoints([]);
		setLiveStatus("Loading live data…");
		setLiveError(null);

		void seed();
		return () => {
			cancelled = true;
		};
	}, [containerId]);

	// ── Effect 2: Live polling every 5 s (runs regardless of viewMode) ──────────
	// We keep polling even in bundle view so switching back to Live is instant.
	const latestTimeRef = useRef<string | null>(null);

	useEffect(() => {
		if (!seeded) return; // Don't start polling until the seed fetch completes

		let cancelled = false;

		async function poll(): Promise<void> {
			try {
				const data = await apiFetch<LatestResponse>(`/api/metrics/latest?container=${encodeURIComponent(containerId)}`);
				if (cancelled) return;
				if (data.point === null) return; // collector hasn't run yet

				// De-duplicate: skip if this point's timestamp is the same as last.
				if (data.point.time === latestTimeRef.current) return;
				latestTimeRef.current = data.point.time;

				setLivePoints((prev) => {
					const next = [...prev, data.point!];
					// Keep the rolling window bounded.
					return next.slice(-LIVE_MAX_POINTS);
				});
				setLiveStatus("Live — updating every 5s.");
				setLiveError(null);
			} catch (err) {
				if (cancelled) return;
				setLiveError(err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach server.");
			}
		}

		const timerId = window.setInterval(poll, LIVE_POLL_MS);
		// First poll immediately (don't wait LIVE_POLL_MS before first update)
		void poll();

		return () => {
			cancelled = true;
			window.clearInterval(timerId);
		};
	}, [containerId, seeded]);

	// ── Effect 3: Bundle history fetch (only in 1h / 24h / 7d modes) ────────────
	useEffect(() => {
		if (viewMode === "live") {
			setBundleHistory(null);
			setBundleError(null);
			setBundleStatus("");
			return;
		}

		let cancelled = false;

		async function loadBundle(): Promise<void> {
			setBundleStatus(`Loading ${viewMode} bundle…`);
			setBundleError(null);
			try {
				const data = await apiFetch<HistoryResponse>(
					`/api/containers/${encodeURIComponent(containerId)}/history?window=${viewMode}`,
				);
				if (cancelled) return;
				setBundleHistory(data);
				if (data.points.length > 0) {
					setBundleStatus(
						`Showing ${data.points.length} sample${data.points.length === 1 ? "" : "s"} from the last ${viewMode}.`,
					);
				} else {
					// Bundle is empty — guide the user back to live view where data exists.
					setBundleStatus(
						`No bundled samples recorded in the last ${viewMode} yet. Switch to "Live (5s)" to see real-time data.`,
					);
				}
				setBundleError(null);
			} catch (err) {
				if (cancelled) return;
				setBundleError(err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach server.");
				setBundleStatus("Bundle data unavailable.");
			}
		}

		void loadBundle();
		const timerId = window.setInterval(loadBundle, BUNDLE_REFRESH_MS);

		return () => {
			cancelled = true;
			window.clearInterval(timerId);
		};
	}, [containerId, viewMode]);

	// ── Derived series ─────────────────────────────────────────────────────────

	// In live mode use livePoints; in bundle modes use bundleHistory.points.
	const points = viewMode === "live" ? livePoints : (bundleHistory?.points ?? []);

	const cpuSeries = useMemo(() => buildCpuSeries(points), [points]);
	const ramSeries = useMemo(() => buildValueSeries(points, (r) => r.ram_used_mb), [points]);
	const diskSeries = useMemo(
		() =>
			buildValueSeries(
				points,
				(r) => r.disk_used_mb,
				(v) => v / 1024,
			),
		[points],
	);
	const networkSeries = useMemo(() => buildNetworkSeries(points), [points]);

	// Use the most recent live point for the current-value readouts in the headers.
	const currentPoint = latestLivePoint ?? points.at(-1) ?? null;
	const latestCpuPercent = cpuSeries.at(-1)?.value ?? null;
	const latestNetworkRate = networkSeries.at(-1)?.value ?? null;

	const statusMessage = viewMode === "live" ? liveStatus : bundleStatus;
	const error = viewMode === "live" ? liveError : bundleError;

	// ── Render ─────────────────────────────────────────────────────────────────

	return (
		<section className='resource-graphs' aria-label='Resource usage graphs'>
			{/* ── Header: title + view-mode toggle ── */}
			<div className='resource-graphs-header'>
				<div>
					<h3>Resource graphs</h3>
					<p className='resource-graphs-status'>{statusMessage}</p>
				</div>
				<div className='resource-window-group' role='group' aria-label='View mode'>
					{VIEW_MODES.map((m) => (
						<button
							type='button'
							key={m.value}
							className={viewMode === m.value ? "active" : ""}
							aria-pressed={String(m.value === viewMode)}
							onClick={() => setViewMode(m.value)}>
							{m.label}
						</button>
					))}
				</div>
			</div>

			{error && <p className='resource-graphs-error'>{error}</p>}

			{/* ── Four graph cards ── */}
			<div className='graph-grid'>
				{/* CPU */}
				<article className='graph-card'>
					<header>
						<div>
							<h4>CPU</h4>
							<p>
								{currentPoint === null && latestCpuPercent === null ?
									"Waiting for samples"
								: latestCpuPercent === null ?
									"Measuring…"
								:	formatPercent(latestCpuPercent)}{" "}
								of one core
							</p>
						</div>
						<span>{cpuAllocated.toFixed(0)} cores allocated</span>
					</header>
					{cpuSeries.length > 0 ?
						<HistoryChart points={cpuSeries} height={120} />
					:	<p className='graph-empty'>
							{viewMode === "live" ?
								"Collecting samples — chart updates every 5 s."
							:	"No bundled CPU samples in this window."}
						</p>
					}
				</article>

				{/* RAM */}
				<article className='graph-card'>
					<header>
						<div>
							<h4>RAM</h4>
							<p>
								{currentPoint === null ?
									"Waiting for samples"
								:	`${currentPoint.ram_used_mb.toFixed(0)} / ${ramAllocatedMb.toFixed(0)} MB`}
							</p>
						</div>
						<span>{ramAllocatedMb.toFixed(0)} MB allocated</span>
					</header>
					{ramSeries.length > 0 ?
						<HistoryChart points={ramSeries} height={120} />
					:	<p className='graph-empty'>
							{viewMode === "live" ?
								"Collecting samples — chart updates every 5 s."
							:	"No bundled RAM samples in this window."}
						</p>
					}
				</article>

				{/* Disk */}
				<article className='graph-card'>
					<header>
						<div>
							<h4>Disk</h4>
							<p>
								{currentPoint === null ?
									"Waiting for samples"
								:	`${(currentPoint.disk_used_mb / 1024).toFixed(2)} / ${diskAllocatedGb.toFixed(0)} GB`}
							</p>
						</div>
						<span>{diskAllocatedGb.toFixed(0)} GB allocated</span>
					</header>
					{diskSeries.length > 0 ?
						<HistoryChart points={diskSeries} height={120} />
					:	<p className='graph-empty'>
							{viewMode === "live" ?
								"Collecting samples — chart updates every 5 s."
							:	"No bundled disk samples in this window."}
						</p>
					}
				</article>

				{/* Network */}
				<article className='graph-card'>
					<header>
						<div>
							<h4>Network</h4>
							<p>
								{currentPoint === null && latestNetworkRate === null ?
									"Waiting for samples"
								: latestNetworkRate === null ?
									"Measuring…"
								:	formatBytes(latestNetworkRate)}{" "}
								total throughput
							</p>
						</div>
						<span>RX + TX</span>
					</header>
					{networkSeries.length > 0 ?
						<HistoryChart points={networkSeries} height={120} />
					:	<p className='graph-empty'>
							{viewMode === "live" ?
								"Collecting samples — chart updates every 5 s."
							:	"No bundled network samples in this window."}
						</p>
					}
				</article>
			</div>
		</section>
	);
}
