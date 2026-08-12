import { useEffect, useRef, useState } from "react";
import { apiFetch, ApiError } from "../lib/api";
import type { MetricPoint } from "./MetricTile";

interface ContainerDetailSummaryProps {
	containerId: string;
	lxdName: string;
	image: string;
	osImage: string;
	ipAddresses: string[];
	architecture: string;
	createdAt: string;
	initialState: string;
	ramAllocatedMb: number;
	cpuAllocated: number;
	diskAllocatedGb: number;
}

interface ContainerDetailResponse {
	lxd_status: string;
	lxd_image: string;
	lxd_ip_addresses: string[];
	lxd_architecture: string;
	lxd_created_at: string;
}

function formatBytes(bytes: number): string {
	if (bytes < 1024) return `${Math.round(bytes)} B`;
	if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
	if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
	return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function formatDuration(ms: number): string {
	if (!Number.isFinite(ms) || ms < 0) {
		return "Unknown";
	}

	const seconds = Math.floor(ms / 1000);
	const minutes = Math.floor(seconds / 60);
	const hours = Math.floor(minutes / 60);
	const days = Math.floor(hours / 24);

	if (days > 0) {
		return `${days}d ${hours % 24}h`;
	}
	if (hours > 0) {
		return `${hours}h ${minutes % 60}m`;
	}
	if (minutes > 0) {
		return `${minutes}m ${seconds % 60}s`;
	}
	return `${seconds}s`;
}

function computeCpuPercent(previous: MetricPoint, current: MetricPoint): number | null {
	const elapsedMs = Date.parse(current.time) - Date.parse(previous.time);
	if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return null;

	const busyNs = current.cpu_usage_ns - previous.cpu_usage_ns;
	if (busyNs < 0) return null;

	return (busyNs / (elapsedMs * 1_000_000)) * 100;
}

export default function ContainerDetailSummary({
	containerId,
	lxdName,
	image,
	osImage,
	ipAddresses,
	architecture,
	createdAt,
	initialState,
	ramAllocatedMb,
	cpuAllocated,
	diskAllocatedGb,
}: ContainerDetailSummaryProps) {
	const [point, setPoint] = useState<MetricPoint | null>(null);
	const [cpuPercent, setCpuPercent] = useState<number | null>(null);
	const [status, setStatus] = useState(initialState);
	const [currentOsImage, setCurrentOsImage] = useState(osImage);
	const [currentIpAddresses, setCurrentIpAddresses] = useState(ipAddresses);
	const [limitRamMb, setLimitRamMb] = useState<number>(ramAllocatedMb);
	const [limitCpu, setLimitCpu] = useState<number>(cpuAllocated);
	const [limitDiskGb, setLimitDiskGb] = useState<number>(diskAllocatedGb);
	const [lastUpdated, setLastUpdated] = useState<string | null>(null);
	const [error, setError] = useState<string | null>(null);
	const [actionMessage, setActionMessage] = useState<string | null>(null);
	const [busy, setBusy] = useState(false);
	const previousPoint = useRef<MetricPoint | null>(null);

	const refreshMetadata = async (): Promise<void> => {
		try {
			const details = await apiFetch<ContainerDetailResponse>(`/api/containers/${encodeURIComponent(containerId)}`);
			setStatus(details.lxd_status);
			setCurrentOsImage(details.lxd_image || image);
			setCurrentIpAddresses(details.lxd_ip_addresses || []);
		} catch {
			// Keep the last known metadata rather than overwriting it with an error.
		}
	};

	const handleAction = async (action: "start" | "stop" | "restart" | "freeze" | "unfreeze") => {
		setBusy(true);
		setActionMessage(`Sending ${action}…`);

		try {
			await apiFetch(`/api/containers/${encodeURIComponent(containerId)}`, {
				method: "PATCH",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ action }),
			});

			await refreshMetadata();
			setActionMessage(`Action ${action} successful.`);
		} catch (err) {
			setActionMessage(err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach the server.");
		} finally {
			setBusy(false);
			window.setTimeout(() => setActionMessage(null), 3000);
		}
	};

	const handleLimitUpdate = async (): Promise<void> => {
		setBusy(true);
		setActionMessage("Updating limits…");

		try {
			await apiFetch(`/api/containers/${encodeURIComponent(containerId)}`, {
				method: "PATCH",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					limits: {
						ram_mb: limitRamMb,
						cpu: limitCpu,
						disk_gb: limitDiskGb,
					},
				}),
			});

			setActionMessage("Resource limits updated successfully.");
		} catch (err) {
			setActionMessage(err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach the server.");
		} finally {
			setBusy(false);
			window.setTimeout(() => setActionMessage(null), 3000);
		}
	};

	const handleDelete = async (): Promise<void> => {
		if (!window.confirm(`Delete container '${lxdName}'? This action is permanent.`)) {
			return;
		}

		setBusy(true);
		setActionMessage("Deleting container…");

		try {
			await apiFetch(`/api/containers/${encodeURIComponent(containerId)}`, {
				method: "DELETE",
			});

			window.location.replace("/admin");
		} catch (err) {
			setActionMessage(err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach the server.");
		} finally {
			setBusy(false);
		}
	};

	useEffect(() => {
		let cancelled = false;

		async function poll(): Promise<void> {
			try {
				const data = await apiFetch<{ container_id: string; point: MetricPoint | null }>(
					`/api/metrics/latest?container=${encodeURIComponent(containerId)}`,
				);

				if (cancelled) return;

				if (data.point === null) {
					setPoint(null);
					setError(null);
					setLastUpdated(null);
					return;
				}

				if (previousPoint.current) {
					const nextCpu = computeCpuPercent(previousPoint.current, data.point);
					if (nextCpu !== null) setCpuPercent(nextCpu);
				}

				previousPoint.current = data.point;
				setPoint(data.point);
				setError(null);
				setLastUpdated(`Updated ${Math.round((Date.now() - Date.parse(data.point.time)) / 1000)}s ago`);
			} catch (err) {
				if (cancelled) return;
				setError(err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach the server.");
			}
		}

		void poll();
		const timerId = window.setInterval(poll, 5000);
		return () => {
			cancelled = true;
			window.clearInterval(timerId);
		};
	}, [containerId]);

	const uptimeMs = Date.now() - Date.parse(createdAt);

	return (
		<section className='container-summary'>
			<div className='summary-grid'>
				<div className='summary-panel'>
					<div className='summary-heading'>
						<h2>{lxdName}</h2>
						<span className={`status-chip status-${status.toLowerCase()}`}>{status}</span>
					</div>

					<dl>
						<dt>OS image</dt>
						<dd>{currentOsImage || image}</dd>
						<dt>IP address</dt>
						<dd>{currentIpAddresses.length > 0 ? currentIpAddresses.join(", ") : "None"}</dd>
						<dt>Architecture</dt>
						<dd>{architecture}</dd>
						<dt>Created</dt>
						<dd>{new Date(createdAt).toLocaleString()}</dd>
						<dt>Uptime</dt>
						<dd>{status === "Running" ? formatDuration(uptimeMs) : "Stopped"}</dd>
					</dl>
				</div>

				<div className='live-panel'>
					<h3>Live usage</h3>
					{error ?
						<p className='error'>{error}</p>
					: point === null ?
						<p>No metrics have been collected for this container yet.</p>
					:	<dl>
							<dt>CPU</dt>
							<dd>{cpuPercent === null ? "measuring…" : `${cpuPercent.toFixed(1)}%`}</dd>
							<dt>RAM</dt>
							<dd>
								{point.ram_used_mb.toFixed(0)} / {ramAllocatedMb} MB
							</dd>
							<dt>Disk</dt>
							<dd>
								{(point.disk_used_mb / 1024).toFixed(2)} / {diskAllocatedGb} GB
							</dd>
							<dt>Network</dt>
							<dd>
								↓ {formatBytes(point.net_rx_bytes)} ↑ {formatBytes(point.net_tx_bytes)}
							</dd>
							<dt>Processes</dt>
							<dd>{point.pid_count.toFixed(0)}</dd>
						</dl>
					}

					<div className='action-row'>
						<button type='button' onClick={() => void handleAction("start")} disabled={busy}>
							Start
						</button>
						<button type='button' onClick={() => void handleAction("stop")} disabled={busy}>
							Stop
						</button>
						<button type='button' onClick={() => void handleAction("restart")} disabled={busy}>
							Restart
						</button>
						<button type='button' onClick={() => void handleAction("freeze")} disabled={busy}>
							Freeze
						</button>
						<button type='button' onClick={() => void handleAction("unfreeze")} disabled={busy}>
							Unfreeze
						</button>
					</div>

					<div className='limit-editor'>
						<h4>Resource limits</h4>
						<label>
							RAM (MB)
							<input
								type='number'
								min={256}
								step={256}
								value={limitRamMb}
								onChange={(event) => setLimitRamMb(Number(event.target.value))}
								disabled={busy}
							/>
						</label>
						<label>
							CPU
							<input
								type='number'
								min={1}
								step={1}
								value={limitCpu}
								onChange={(event) => setLimitCpu(Number(event.target.value))}
								disabled={busy}
							/>
						</label>
						<label>
							Disk (GB)
							<input
								type='number'
								min={1}
								step={1}
								value={limitDiskGb}
								onChange={(event) => setLimitDiskGb(Number(event.target.value))}
								disabled={busy}
							/>
						</label>
						<button type='button' onClick={() => void handleLimitUpdate()} disabled={busy}>
							Apply limits
						</button>
					</div>

					<div className='danger-row'>
						<button type='button' className='danger-button' onClick={() => void handleDelete()} disabled={busy}>
							Delete container
						</button>
					</div>

					{actionMessage && <p className='action-message'>{actionMessage}</p>}
				</div>
			</div>
		</section>
	);
}
