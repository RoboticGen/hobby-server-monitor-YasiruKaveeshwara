/**
 * Container resource usage graphs component.
 *
 * Provides real-time and historical time-series graphs for:
 * - CPU (%)
 * - RAM (MB used vs limit)
 * - Disk (GB used vs limit)
 * - Network throughput (bytes/s RX + TX)
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, ApiError } from "../lib/api";
import HistoryChart, { type HistoryPoint } from "./HistoryChart";

export type ViewMode = "live" | "1h" | "24h" | "7d";

export interface ContainerResourceGraphsProps {
	containerId: string;
	ramAllocatedMb: number;
	cpuAllocated: number;
	diskAllocatedGb: number;
}

interface HistoryRow {
	time: string;
	cpu_usage_ns: number;
	ram_used_mb: number;
	ram_peak_mb: number;
	disk_used_mb: number;
	net_rx_bytes: number;
	net_tx_bytes: number;
	pid_count: number;
}

interface RecentResponse {
	container_id: string;
	count: number;
	points: HistoryRow[];
}

interface LatestResponse {
	container_id: string;
	point: HistoryRow | null;
}

interface HistoryResponse {
	container_id: string;
	window: string;
	points: HistoryRow[];
}

function formatBytes(bytes: number): string {
	if (bytes < 1024) return `${Math.round(bytes)} B/s`;
	if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB/s`;
	if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB/s`;
	return `${(bytes / 1024 ** 3).toFixed(2)} GB/s`;
}

function formatPercent(value: number): string {
	return `${value.toFixed(1)}%`;
}

function computeCpuPercent(previous: HistoryRow, current: HistoryRow): number | null {
	const elapsedMs = Date.parse(current.time) - Date.parse(previous.time);
	if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return null;

	const busyNs = current.cpu_usage_ns - previous.cpu_usage_ns;
	if (busyNs < 0) return null;

	return (busyNs / (elapsedMs * 1_000_000)) * 100;
}

function computeRate(previous: HistoryRow, current: HistoryRow, field: "net_rx_bytes" | "net_tx_bytes"): number | null {
	const elapsedMs = Date.parse(current.time) - Date.parse(previous.time);
	if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return null;

	const delta = current[field] - previous[field];
	if (!Number.isFinite(delta) || delta < 0) return null;

	return delta / (elapsedMs / 1000);
}

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

const LIVE_MAX_POINTS = 720;
const LIVE_POLL_MS = 5_000;
const BUNDLE_REFRESH_MS = 30_000;

// A CPU percentage and a network rate are both differences between two counter
// readings, so the live view needs two points before it can draw either. Until
// then it asks the API for a fresh sample (`fresh=1`) on a faster timer: a
// container that has only just started would otherwise show "Measuring…" for
// up to two collector intervals. The server ignores `fresh` once two points
// exist, so this cannot keep pulling on LXD — see LatestMetricsResource.
const CHARTABLE_MINIMUM = 2;
const COLD_START_POLL_MS = 1_000;

const VIEW_MODES: Array<{ value: ViewMode; label: string }> = [
	{ value: "live", label: "Live (5s)" },
	{ value: "1h", label: "1h Window" },
	{ value: "24h", label: "24h Window" },
	{ value: "7d", label: "7d Window" },
];

export default function ContainerResourceGraphs({
	containerId,
	ramAllocatedMb,
	cpuAllocated,
	diskAllocatedGb,
}: ContainerResourceGraphsProps) {
	const [viewMode, setViewMode] = useState<ViewMode>("live");
	const [livePoints, setLivePoints] = useState<HistoryRow[]>([]);
	const [seeded, setSeeded] = useState(false);
	const [liveError, setLiveError] = useState<string | null>(null);
	const [liveStatus, setLiveStatus] = useState<string>("Loading telemetry streams…");

	const [bundleHistory, setBundleHistory] = useState<HistoryResponse | null>(null);
	const [bundleError, setBundleError] = useState<string | null>(null);
	const [bundleStatus, setBundleStatus] = useState<string>("");

	const latestLivePoint = livePoints.at(-1) ?? null;

	// Newest timestamp already in livePoints, tracked separately from state so
	// the poll callback can dedupe without being re-created on every point.
	const latestTimeRef = useRef<string | null>(null);

	useEffect(() => {
		let cancelled = false;

		async function seed(): Promise<void> {
			try {
				const data = await apiFetch<RecentResponse>(
					`/api/metrics/recent?container=${encodeURIComponent(containerId)}&limit=120`,
				);
				if (cancelled) return;

				if (data.points.length > 0) {
					setLivePoints(data.points);
					// Remember the newest seeded timestamp, so the first poll
					// does not append a point the seed already contains. A
					// duplicate would sit in the series with a zero time delta
					// and silently drop a CPU/network sample, since both are
					// computed by differencing against the previous point.
					latestTimeRef.current = data.points[data.points.length - 1].time;
					setLiveStatus(`Live stream (${data.points.length} samples buffered)`);
				} else {
					latestTimeRef.current = null;
					setLiveStatus("Awaiting first collector metrics…");
				}
				setSeeded(true);
				setLiveError(null);
			} catch (err) {
				if (cancelled) return;
				setLiveError(err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach server.");
				setLiveStatus("Could not pre-load live data.");
				setSeeded(true);
			}
		}

		setSeeded(false);
		setLivePoints([]);
		setLiveStatus("Loading live telemetry…");
		setLiveError(null);

		void seed();
		return () => {
			cancelled = true;
		};
	}, [containerId]);

	// Below the two-point floor the live view cannot draw a rate, so it polls
	// faster and asks the server for a fresh sample. Flips to false once, which
	// re-arms the interval below at its normal cadence.
	const isColdStart = livePoints.length < CHARTABLE_MINIMUM;

	useEffect(() => {
		if (!seeded) return;

		let cancelled = false;

		async function poll(): Promise<void> {
			try {
				const query = new URLSearchParams({ container: containerId });
				if (isColdStart) query.set("fresh", "1");

				const data = await apiFetch<LatestResponse>(`/api/metrics/latest?${query}`);
				if (cancelled) return;

				if (data.point === null) {
					// Nothing stored yet and nothing sampleable — the container
					// is not running, or LXD could not be read. Say so instead
					// of leaving the toolbar on its pre-load message while all
					// four tiles read "Measuring…".
					setLiveStatus("No samples yet — is the container running?");
					setLiveError(null);
					return;
				}

				if (data.point.time === latestTimeRef.current) return;
				latestTimeRef.current = data.point.time;

				setLivePoints((prev) => {
					const next = [...prev, data.point!];
					return next.slice(-LIVE_MAX_POINTS);
				});
				setLiveStatus(
					isColdStart ?
						"First samples in — starting live stream…"
					:	`Live stream active (polling ${LIVE_POLL_MS / 1000}s)`,
				);
				setLiveError(null);
			} catch (err) {
				if (cancelled) return;
				setLiveError(err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach server.");
			}
		}

		const timerId = window.setInterval(poll, isColdStart ? COLD_START_POLL_MS : LIVE_POLL_MS);
		void poll();

		return () => {
			cancelled = true;
			window.clearInterval(timerId);
		};
	}, [containerId, seeded, isColdStart]);

	useEffect(() => {
		if (viewMode === "live") {
			setBundleHistory(null);
			setBundleError(null);
			setBundleStatus("");
			return;
		}

		let cancelled = false;

		async function loadBundle(): Promise<void> {
			setBundleStatus(`Aggregating ${viewMode} historical metrics…`);
			setBundleError(null);
			try {
				const data = await apiFetch<HistoryResponse>(
					`/api/containers/${encodeURIComponent(containerId)}/history?window=${viewMode}`,
				);
				if (cancelled) return;
				setBundleHistory(data);
				if (data.points.length > 0) {
					setBundleStatus(`Historical aggregate: ${data.points.length} samples over past ${viewMode}.`);
				} else {
					setBundleStatus(`No aggregated samples in past ${viewMode}. Switch to Live view.`);
				}
				setBundleError(null);
			} catch (err) {
				if (cancelled) return;
				setBundleError(err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach server.");
				setBundleStatus("History rollup unavailable.");
			}
		}

		void loadBundle();
		const timerId = window.setInterval(loadBundle, BUNDLE_REFRESH_MS);

		return () => {
			cancelled = true;
			window.clearInterval(timerId);
		};
	}, [containerId, viewMode]);

	const points = viewMode === "live" ? livePoints.slice(-30) : (bundleHistory?.points ?? []);

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

	const currentPoint = latestLivePoint ?? points.at(-1) ?? null;
	const latestCpuPercent = cpuSeries.at(-1)?.value ?? null;
	const latestNetworkRate = networkSeries.at(-1)?.value ?? null;

	const statusMessage = viewMode === "live" ? liveStatus : bundleStatus;
	const error = viewMode === "live" ? liveError : bundleError;

	return (
		<section className='resource-graphs-section' aria-label='Resource usage graphs'>
			<div className='graphs-toolbar'>
				<div className='toolbar-left'>
					{viewMode === "live" ?
						<span className='live-badge-ticker'>
							<span className='live-dot'></span> LIVE (5s)
						</span>
					:	<span className='pulse-dot'></span>}
					<span className='toolbar-status'>{statusMessage}</span>
				</div>
				<div className='view-mode-selector' role='group' aria-label='View mode'>
					{VIEW_MODES.map((m) => (
						<button
							type='button'
							key={m.value}
							className={`mode-btn ${viewMode === m.value ? "active" : ""}`}
							aria-pressed={viewMode === m.value}
							onClick={() => setViewMode(m.value)}>
							{m.label}
						</button>
					))}
				</div>
			</div>

			{error && <div className='alert alert-error'>{error}</div>}

			<div className='graphs-grid'>
				{/* CPU Chart */}
				<article className='graph-card glass-card'>
					<header className='graph-card-header'>
						<div>
							<h4>CPU Load</h4>
							<p className='graph-stat-val mono'>
								{latestCpuPercent !== null ?
									formatPercent(latestCpuPercent)
								: currentPoint === null ?
									"Measuring…"
								:	"Awaiting 2nd sample…"}
							</p>
						</div>
						<span className='graph-limit-tag'>{cpuAllocated} core(s)</span>
					</header>
					{cpuSeries.length > 0 ?
						<HistoryChart
							points={cpuSeries}
							height={150}
							color='#0284c7'
							formatValue={(v) => `${v.toFixed(1)}%`}
							isLive={viewMode === "live"}
							timeWindowLabel={viewMode}
						/>
					:	<div className='graph-empty-state'>
							<span>Collecting telemetry…</span>
						</div>
					}
				</article>

				{/* RAM Chart */}
				<article className='graph-card glass-card'>
					<header className='graph-card-header'>
						<div>
							<h4>RAM Utilization</h4>
							<p className='graph-stat-val mono'>
								{currentPoint === null ? "Measuring…" : `${currentPoint.ram_used_mb.toFixed(0)} / ${ramAllocatedMb} MB`}
							</p>
						</div>
						<span className='graph-limit-tag'>{ramAllocatedMb} MB limit</span>
					</header>
					{ramSeries.length > 0 ?
						<HistoryChart
							points={ramSeries}
							height={150}
							color='#059669'
							formatValue={(v) => `${v.toFixed(0)} MB`}
							isLive={viewMode === "live"}
							timeWindowLabel={viewMode}
						/>
					:	<div className='graph-empty-state'>
							<span>Collecting telemetry…</span>
						</div>
					}
				</article>

				{/* Disk Chart */}
				<article className='graph-card glass-card'>
					<header className='graph-card-header'>
						<div>
							<h4>Disk Storage</h4>
							<p className='graph-stat-val mono'>
								{currentPoint === null ?
									"Measuring…"
								:	`${(currentPoint.disk_used_mb / 1024).toFixed(2)} / ${diskAllocatedGb} GB`}
							</p>
						</div>
						<span className='graph-limit-tag'>{diskAllocatedGb} GB limit</span>
					</header>
					{diskSeries.length > 0 ?
						<HistoryChart
							points={diskSeries}
							height={150}
							color='#8b5cf6'
							formatValue={(v) => `${v.toFixed(2)} GB`}
							isLive={viewMode === "live"}
							timeWindowLabel={viewMode}
						/>
					:	<div className='graph-empty-state'>
							<span>Collecting telemetry…</span>
						</div>
					}
				</article>

				{/* Network Chart */}
				<article className='graph-card glass-card'>
					<header className='graph-card-header'>
						<div>
							<h4>Network I/O</h4>
							<p className='graph-stat-val mono'>
								{latestNetworkRate !== null ?
									formatBytes(latestNetworkRate)
								: currentPoint === null ?
									"Measuring…"
								:	"Awaiting 2nd sample…"}
							</p>
						</div>
						<span className='graph-limit-tag'>RX + TX Aggregate</span>
					</header>
					{networkSeries.length > 0 ?
						<HistoryChart
							points={networkSeries}
							height={150}
							color='#d97706'
							formatValue={formatBytes}
							isLive={viewMode === "live"}
							timeWindowLabel={viewMode}
						/>
					:	<div className='graph-empty-state'>
							<span>Collecting telemetry…</span>
						</div>
					}
				</article>
			</div>
		</section>
	);
}
