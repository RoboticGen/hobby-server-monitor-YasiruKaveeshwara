/**
 * Live metric tile for a single container.
 *
 * Polls the backend for the newest sample and re-renders in place. One tile
 * per container is mounted by the dashboard, each polling independently.
 */
import { useEffect, useRef, useState } from "react";
import { apiFetch, ApiError } from "../lib/api";

const POLL_INTERVAL_MS = 5_000;

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

interface LatestMetricsResponse {
	container_id: string;
	point: MetricPoint | null;
}

export interface MetricTileProps {
	containerId: string;
	name: string;
	state: string;
	osImage?: string;
	ipAddresses?: string[];
	createdAt?: string;
	processCount?: number;
	ramAllocatedMb: number;
	diskAllocatedGb: number;
	initialPoint?: MetricPoint | null;
}

function formatBytes(bytes: number): string {
	if (bytes < 1024) return `${Math.round(bytes)} B`;
	if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
	if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
	return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function computeCpuPercent(previous: MetricPoint, current: MetricPoint): number | null {
	const elapsedMs = Date.parse(current.time) - Date.parse(previous.time);
	if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return null;

	const busyNs = current.cpu_usage_ns - previous.cpu_usage_ns;
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

function getPercentValue(part: number, whole: number): number {
	if (!Number.isFinite(whole) || whole <= 0) return 0;
	return Math.min(100, Math.max(0, (part / whole) * 100));
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
	osImage = "Linux",
	ipAddresses = [],
	createdAt = "",
	processCount = 0,
	ramAllocatedMb,
	diskAllocatedGb,
	initialPoint = null,
}: MetricTileProps) {
	const [point, setPoint] = useState<MetricPoint | null>(initialPoint);
	const [cpuPercent, setCpuPercent] = useState<number | null>(null);
	const [rxRate, setRxRate] = useState<number | null>(null);
	const [txRate, setTxRate] = useState<number | null>(null);
	const [error, setError] = useState<string | null>(null);

	const previousPoint = useRef<MetricPoint | null>(initialPoint);

	useEffect(() => {
		let cancelled = false;

		async function poll(): Promise<void> {
			try {
				// `fresh=1` while this tile has no previous point to difference
				// against: a container that has only just started has nothing
				// stored, and CPU/network are rates that need two readings. The
				// server honours it only below that two-point floor, so an
				// established container never puts this on LXD.
				const query = new URLSearchParams({ container: containerId });
				if (previousPoint.current === null) query.set("fresh", "1");

				const data = await apiFetch<LatestMetricsResponse>(`/api/metrics/latest?${query}`);
				if (cancelled) return;

				if (data.point === null) {
					setError(null);
					return;
				}

				const nextCpu = previousPoint.current ? computeCpuPercent(previousPoint.current, data.point) : null;
				const nextRxRate =
					previousPoint.current ? computeRate(previousPoint.current, data.point, "net_rx_bytes") : null;
				const nextTxRate =
					previousPoint.current ? computeRate(previousPoint.current, data.point, "net_tx_bytes") : null;

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

		void poll();
		const timerId = setInterval(poll, POLL_INTERVAL_MS);

		return () => {
			cancelled = true;
			clearInterval(timerId);
		};
	}, [containerId]);

	const normalizedState = (state || "Unknown").toLowerCase();
	const ramPercentNum = point ? getPercentValue(point.ram_used_mb, ramAllocatedMb) : 0;
	const diskUsedGb = point ? point.disk_used_mb / 1024 : 0;
	const diskPercentNum = point ? getPercentValue(diskUsedGb, diskAllocatedGb) : 0;

	return (
		<article className={`metric-tile ${normalizedState}`}>
			<header className='tile-header'>
				<div className='title-area'>
					<h3 className='tile-title' title={name}>
						{name}
					</h3>
					<span className={`status-badge ${normalizedState}`}>{state === "Unknown" ? "Error" : state}</span>
				</div>

				<div className='tile-meta'>
					<span className='meta-item ip-text'>{ipAddresses.length > 0 ? ipAddresses[0] : "No IP"}</span>
					<span className='meta-separator'>•</span>
					<span className='meta-item'>{osImage || "Container"}</span>
					<span className='meta-separator'>•</span>
					<span className='meta-item'>
						Up{" "}
						{state === "Running" ?
							formatUptime(createdAt)
						: state === "Frozen" ?
							"Frozen"
						: state === "Stopped" ?
							"Stopped"
						:	"Unknown"}
					</span>
					<span className='meta-separator'>•</span>
					<span className='meta-item'>{processCount.toFixed(0)} procs</span>
				</div>
			</header>

			{error && (
				<div className='tile-error-banner' role='status'>
					<span className='warning-icon'>⚠️</span> Live telemetry paused ({error})
				</div>
			)}

			{point === null ?
				<div className='tile-empty'>
					<span className='empty-pulse'></span>
					<p>Awaiting metric telemetry…</p>
				</div>
			:	<div className='metrics-grid'>
					{/* CPU Row */}
					<div className='metric-row'>
						<div className='metric-label-group'>
							<span className='metric-name'>CPU</span>
							<span className='metric-val mono'>
								{cpuPercent === null ? "measuring…" : `${cpuPercent.toFixed(1)}%`}
							</span>
						</div>
						<div className='progress-bar-container'>
							<div
								className={`progress-bar-fill ${cpuPercent && cpuPercent > 85 ? "warning" : ""}`}
								style={{ width: `${Math.min(100, Math.max(2, cpuPercent ?? 0))}%` }}
							/>
						</div>
					</div>

					{/* RAM Row */}
					<div className='metric-row'>
						<div className='metric-label-group'>
							<span className='metric-name'>RAM</span>
							<span className='metric-val mono'>
								{point.ram_used_mb.toFixed(0)} MB
								<span className='metric-total'> / {ramAllocatedMb ? `${ramAllocatedMb} MB` : "∞"}</span>
								<span className='metric-pct'> ({formatPercent(point.ram_used_mb, ramAllocatedMb)})</span>
							</span>
						</div>
						<div className='progress-bar-container'>
							<div
								className={`progress-bar-fill ${ramPercentNum > 85 ? "warning" : ""}`}
								style={{ width: `${Math.max(2, ramPercentNum)}%` }}
							/>
						</div>
					</div>

					{/* Disk Row */}
					<div className='metric-row'>
						<div className='metric-label-group'>
							<span className='metric-name'>DISK</span>
							<span className='metric-val mono'>
								{diskUsedGb.toFixed(2)} GB
								<span className='metric-total'> / {diskAllocatedGb ? `${diskAllocatedGb} GB` : "∞"}</span>
								<span className='metric-pct'> ({formatPercent(diskUsedGb, diskAllocatedGb)})</span>
							</span>
						</div>
						<div className='progress-bar-container'>
							<div
								className={`progress-bar-fill ${diskPercentNum > 85 ? "warning" : ""}`}
								style={{ width: `${Math.max(2, diskPercentNum)}%` }}
							/>
						</div>
					</div>

					{/* Network Row */}
					<div className='network-row'>
						<div className='net-stat'>
							<span className='net-icon rx'>↓</span>
							<span className='net-rate mono'>{rxRate === null ? "—" : `${formatBytes(rxRate)}/s`}</span>
							<span className='net-total mono'>({formatBytes(point.net_rx_bytes)})</span>
						</div>
						<div className='net-stat'>
							<span className='net-icon tx'>↑</span>
							<span className='net-rate mono'>{txRate === null ? "—" : `${formatBytes(txRate)}/s`}</span>
							<span className='net-total mono'>({formatBytes(point.net_tx_bytes)})</span>
						</div>
					</div>
				</div>
			}
		</article>
	);
}
