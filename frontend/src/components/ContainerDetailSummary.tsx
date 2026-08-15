/**
 * Container detail summary and lifecycle management console.
 *
 * Provides an organized, user-friendly control center:
 * - Instance hero header with live status, IP copy, and lifecycle controls
 * - 4 real-time telemetry metric cards (CPU, RAM, Disk, Network)
 * - Resource limit configuration with interactive sliders/inputs
 * - Container metadata sheet
 */
import { useEffect, useRef, useState } from "react";
import { apiFetch, ApiError } from "../lib/api";
import { toast, showConfirm } from "../lib/alerts";
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
	if (!Number.isFinite(ms) || ms < 0) return "Unknown";

	const seconds = Math.floor(ms / 1000);
	const minutes = Math.floor(seconds / 60);
	const hours = Math.floor(minutes / 60);
	const days = Math.floor(hours / 24);

	if (days > 0) return `${days}d ${hours % 24}h`;
	if (hours > 0) return `${hours}h ${minutes % 60}m`;
	if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
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
	const [actionMessage, setActionMessage] = useState<{ type: "success" | "error" | "info"; text: string } | null>(null);
	const [busy, setBusy] = useState(false);
	const [copiedIp, setCopiedIp] = useState(false);
	const previousPoint = useRef<MetricPoint | null>(null);

	const refreshMetadata = async (): Promise<void> => {
		try {
			const details = await apiFetch<ContainerDetailResponse>(`/api/containers/${encodeURIComponent(containerId)}`);
			setStatus(details.lxd_status);
			setCurrentOsImage(details.lxd_image || image);
			setCurrentIpAddresses(details.lxd_ip_addresses || []);
		} catch {
			// Keep cached metadata
		}
	};

	const executeAction = async (action: "start" | "stop" | "restart" | "freeze" | "unfreeze") => {
		setBusy(true);
		setActionMessage({ type: "info", text: `Dispatching ${action} command…` });
		toast.info(`Dispatching ${action} command for '${lxdName}'…`);

		try {
			await apiFetch(`/api/containers/${encodeURIComponent(containerId)}`, {
				method: "PATCH",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ action }),
			});

			await refreshMetadata();
			setActionMessage({ type: "success", text: `Container ${action} completed successfully.` });
			toast.success(`Container '${lxdName}' ${action}ed successfully.`);
		} catch (err) {
			const errorText = err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach the server.";
			setActionMessage({ type: "error", text: errorText });
			toast.error(err, `Failed to ${action} container`);
		} finally {
			setBusy(false);
			window.setTimeout(() => setActionMessage(null), 4000);
		}
	};

	const handleAction = async (action: "start" | "stop" | "restart" | "freeze" | "unfreeze") => {
		if (action === "stop") {
			const confirmed = await showConfirm({
				title: "Stop Container",
				message: `Are you sure you want to stop container '${lxdName}'? Active services will be halted.`,
				confirmText: "Stop Container",
				isDanger: false,
				iconType: "warning",
				onConfirm: () => executeAction(action),
			});
			if (!confirmed) return;
			return;
		}

		if (action === "restart") {
			const confirmed = await showConfirm({
				title: "Restart Container",
				message: `Restart container '${lxdName}'? All active processes inside the container will reboot.`,
				confirmText: "Restart Container",
				isDanger: false,
				iconType: "info",
				onConfirm: () => executeAction(action),
			});
			if (!confirmed) return;
			return;
		}

		if (action === "freeze") {
			const confirmed = await showConfirm({
				title: "Freeze Container",
				message: `Freeze container '${lxdName}'? All running processes will be paused in memory.`,
				confirmText: "Freeze Container",
				isDanger: false,
				iconType: "warning",
				onConfirm: () => executeAction(action),
			});
			if (!confirmed) return;
			return;
		}

		await executeAction(action);
	};

	const handleLimitUpdate = async (): Promise<void> => {
		setBusy(true);
		setActionMessage({ type: "info", text: "Updating container allocation limits…" });

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

			setActionMessage({ type: "success", text: "Resource limits updated successfully." });
			toast.success(`Resource limits updated for '${lxdName}'.`);
		} catch (err) {
			const errorText = err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach the server.";
			setActionMessage({ type: "error", text: errorText });
			toast.error(err, "Failed to update limits");
		} finally {
			setBusy(false);
			window.setTimeout(() => setActionMessage(null), 4000);
		}
	};

	const handleDelete = async (): Promise<void> => {
		await showConfirm({
			title: "Delete Container",
			message: `Permanently terminate and purge container '${lxdName}'? All local files and configurations will be destroyed. This action cannot be undone.`,
			confirmText: "Delete Container",
			cancelText: "Cancel",
			isDanger: true,
			iconType: "danger",
			onConfirm: async () => {
				setBusy(true);
				setActionMessage({ type: "info", text: "Terminating and purging container…" });
				try {
					await apiFetch(`/api/containers/${encodeURIComponent(containerId)}`, {
						method: "DELETE",
					});
					toast.success(`Container '${lxdName}' deleted successfully.`);
					window.location.replace("/admin");
				} catch (err) {
					const errorText = err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach the server.";
					setActionMessage({ type: "error", text: errorText });
					toast.error(err, "Failed to delete container");
					setBusy(false);
				}
			},
		});
	};

	const copyIp = async (ip: string) => {
		try {
			await navigator.clipboard.writeText(ip);
			setCopiedIp(true);
			toast.success(`IP address copied: ${ip}`, "Copied");
			setTimeout(() => setCopiedIp(false), 2000);
		} catch {
			toast.info(`Selected IP: ${ip}`, "IP Address");
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
					return;
				}

				if (previousPoint.current) {
					const nextCpu = computeCpuPercent(previousPoint.current, data.point);
					if (nextCpu !== null) setCpuPercent(nextCpu);
				}

				previousPoint.current = data.point;
				setPoint(data.point);
			} catch {
				// Keep last point
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
	const normStatus = status.toLowerCase();

	const primaryIp = currentIpAddresses.length > 0 ? currentIpAddresses[0] : null;
	const ramPct =
		point && ramAllocatedMb > 0 ? Math.min(100, Math.max(0, (point.ram_used_mb / ramAllocatedMb) * 100)) : 0;
	const diskUsedGb = point ? point.disk_used_mb / 1024 : 0;
	const diskPct = diskAllocatedGb > 0 ? Math.min(100, Math.max(0, (diskUsedGb / diskAllocatedGb) * 100)) : 0;

	return (
		<div className='container-detail-wrapper'>
			{/* 1. Hero Identity & Lifecycle Action Bar */}
			<div className='container-hero-card glass-card'>
				<div className='hero-left-section'>
					<div className='instance-avatar'>
						<svg width='28' height='28' viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2'>
							<rect x='2' y='2' width='20' height='8' rx='2' ry='2' />
							<rect x='2' y='14' width='20' height='8' rx='2' ry='2' />
							<line x1='6' y1='6' x2='6.01' y2='6' />
							<line x1='6' y1='18' x2='6.01' y2='18' />
						</svg>
					</div>
					<div className='instance-titles'>
						<div className='title-with-badge'>
							<h1>{lxdName}</h1>
							<span className={`status-badge ${normStatus}`}>{status}</span>
						</div>
						<div className='hero-tags-row'>
							{primaryIp ?
								<button
									type='button'
									className='tag-chip ip-chip'
									onClick={() => void copyIp(primaryIp)}
									title='Click to copy IP'>
									<span className='mono'>{primaryIp}</span>
									<span className='chip-copy-icon'>{copiedIp ? "✓" : "📋"}</span>
								</button>
							:	<span className='tag-chip muted-chip'>No IPv4</span>}
							<span className='tag-chip'>{currentOsImage || image}</span>
							<span className='tag-chip'>{architecture}</span>
							<span className='tag-chip uptime-chip'>
								{status === "Running" ? `⏱️ Up ${formatDuration(uptimeMs)}` : `Status: ${status}`}
							</span>
						</div>
					</div>
				</div>

				<div className='hero-actions-section'>
					<button
						type='button'
						className='btn'
						onClick={() => void handleAction("start")}
						disabled={busy || status === "Running"}>
						<span>▶ Start</span>
					</button>
					<button
						type='button'
						className='btn'
						onClick={() => void handleAction("stop")}
						disabled={busy || status === "Stopped"}>
						<span>⏹ Stop</span>
					</button>
					<button
						type='button'
						className='btn'
						onClick={() => void handleAction("restart")}
						disabled={busy || status === "Stopped"}>
						<span>🔄 Restart</span>
					</button>
					<button
						type='button'
						className='btn'
						onClick={() => void handleAction(status === "Frozen" ? "unfreeze" : "freeze")}
						disabled={busy || status === "Stopped"}>
						<span>{status === "Frozen" ? "❄️ Unfreeze" : "❄️ Freeze"}</span>
					</button>
					<button type='button' className='btn danger' onClick={() => void handleDelete()} disabled={busy}>
						<span>🗑 Delete</span>
					</button>
				</div>
			</div>

			{actionMessage && (
				<div
					className={`alert ${
						actionMessage.type === "success" ? "alert-success"
						: actionMessage.type === "error" ? "alert-error"
						: "alert-warning"
					}`}>
					{actionMessage.text}
				</div>
			)}

			{/* 2. Real-time Telemetry Metric Cards */}
			<div className='detail-metrics-grid'>
				{/* CPU Gauge Card */}
				<div className='metric-gauge-card glass-card'>
					<div className='gauge-header'>
						<div className='gauge-icon cpu-icon'>⚡</div>
						<div className='gauge-title-wrap'>
							<span className='gauge-label'>CPU UTILIZATION</span>
							<span className='gauge-main-val mono'>
								{cpuPercent === null ? "Measuring…" : `${cpuPercent.toFixed(1)}%`}
							</span>
						</div>
					</div>
					<div className='progress-bar-container'>
						<div
							className={`progress-bar-fill ${cpuPercent && cpuPercent > 85 ? "warning" : ""}`}
							style={{ width: `${Math.min(100, cpuPercent || 0)}%` }}
						/>
					</div>
					<div className='gauge-footer mono'>
						<span>Allocated: {cpuAllocated} Cores</span>
						<span>{status === "Running" ? "Active" : "Idle"}</span>
					</div>
				</div>

				{/* RAM Memory Card */}
				<div className='metric-gauge-card glass-card'>
					<div className='gauge-header'>
						<div className='gauge-icon ram-icon'>🧠</div>
						<div className='gauge-title-wrap'>
							<span className='gauge-label'>RAM ALLOCATION</span>
							<span className='gauge-main-val mono'>{point ? `${point.ram_used_mb.toFixed(0)} MB` : "0 MB"}</span>
						</div>
					</div>
					<div className='progress-bar-container'>
						<div className={`progress-bar-fill ${ramPct > 85 ? "warning" : ""}`} style={{ width: `${ramPct}%` }} />
					</div>
					<div className='gauge-footer mono'>
						<span>Ceiling: {ramAllocatedMb} MB</span>
						<span>{ramPct.toFixed(0)}% Used</span>
					</div>
				</div>

				{/* Disk Storage Card */}
				<div className='metric-gauge-card glass-card'>
					<div className='gauge-header'>
						<div className='gauge-icon disk-icon'>💾</div>
						<div className='gauge-title-wrap'>
							<span className='gauge-label'>DISK STORAGE</span>
							<span className='gauge-main-val mono'>{point ? `${diskUsedGb.toFixed(2)} GB` : "0 GB"}</span>
						</div>
					</div>
					<div className='progress-bar-container'>
						<div className={`progress-bar-fill ${diskPct > 85 ? "warning" : ""}`} style={{ width: `${diskPct}%` }} />
					</div>
					<div className='gauge-footer mono'>
						<span>Capacity: {diskAllocatedGb} GB</span>
						<span>{diskPct.toFixed(0)}% Used</span>
					</div>
				</div>

				{/* Network & Processes Card */}
				<div className='metric-gauge-card glass-card'>
					<div className='gauge-header'>
						<div className='gauge-icon net-icon'>🌐</div>
						<div className='gauge-title-wrap'>
							<span className='gauge-label'>NETWORK & PROCS</span>
							<span className='gauge-main-val mono'>{point ? `${point.pid_count.toFixed(0)} PIDs` : "0 PIDs"}</span>
						</div>
					</div>
					<div className='net-throughput-row mono'>
						<span>↓ RX: {point ? formatBytes(point.net_rx_bytes) : "0 B"}</span>
						<span>↑ TX: {point ? formatBytes(point.net_tx_bytes) : "0 B"}</span>
					</div>
					<div className='gauge-footer mono'>
						<span>Architecture: {architecture}</span>
						<span>Daemon Synced</span>
					</div>
				</div>
			</div>

			{/* 3. Resource Limits & Configuration Sheet */}
			<div className='details-two-columns'>
				{/* Limit Modifier Box */}
				<div className='config-card glass-card'>
					<div className='card-section-header'>
						<span className='section-icon'>⚙️</span>
						<h3>Resource Limit Adjuster</h3>
					</div>
					<p className='section-desc'>Dynamically scale memory, CPU, and storage headroom.</p>

					<div className='limits-form-grid'>
						<div className='form-group'>
							<label htmlFor={`limit-ram-${containerId}`}>
								<span>RAM Limit</span>
								<span className='mono value-tag'>{limitRamMb} MB</span>
							</label>
							<input
								id={`limit-ram-${containerId}`}
								type='range'
								min={256}
								max={8192}
								step={256}
								value={limitRamMb}
								onChange={(e) => setLimitRamMb(Number(e.target.value))}
								disabled={busy}
							/>
						</div>

						<div className='form-group'>
							<label htmlFor={`limit-cpu-${containerId}`}>
								<span>CPU Cores</span>
								<span className='mono value-tag'>{limitCpu} Cores</span>
							</label>
							<input
								id={`limit-cpu-${containerId}`}
								type='range'
								min={1}
								max={16}
								step={1}
								value={limitCpu}
								onChange={(e) => setLimitCpu(Number(e.target.value))}
								disabled={busy}
							/>
						</div>

						<div className='form-group'>
							<label htmlFor={`limit-disk-${containerId}`}>
								<span>Disk Storage</span>
								<span className='mono value-tag'>{limitDiskGb} GB</span>
							</label>
							<input
								id={`limit-disk-${containerId}`}
								type='range'
								min={5}
								max={100}
								step={1}
								value={limitDiskGb}
								onChange={(e) => setLimitDiskGb(Number(e.target.value))}
								disabled={busy}
							/>
						</div>
					</div>

					<div className='card-action-footer'>
						<button type='button' className='primary' onClick={() => void handleLimitUpdate()} disabled={busy}>
							{busy ? "Saving Changes…" : "Apply Resource Limits"}
						</button>
					</div>
				</div>

				{/* Metadata Sheet */}
				<div className='config-card glass-card'>
					<div className='card-section-header'>
						<span className='section-icon'>📋</span>
						<h3>Container Metadata</h3>
					</div>
					<p className='section-desc'>LXD runtime specifications and identifiers.</p>

					<div className='metadata-table-grid mono'>
						<div className='meta-row'>
							<span className='meta-key'>CONTAINER UUID</span>
							<span className='meta-val'>{containerId}</span>
						</div>
						<div className='meta-row'>
							<span className='meta-key'>LXD INSTANCE</span>
							<span className='meta-val'>{lxdName}</span>
						</div>
						<div className='meta-row'>
							<span className='meta-key'>OS DISTRIBUTION</span>
							<span className='meta-val'>{currentOsImage || image}</span>
						</div>
						<div className='meta-row'>
							<span className='meta-key'>ARCHITECTURE</span>
							<span className='meta-val'>{architecture}</span>
						</div>
						<div className='meta-row'>
							<span className='meta-key'>IPV4 ADDRESS</span>
							<span className='meta-val'>{primaryIp || "Dynamic / Unassigned"}</span>
						</div>
						<div className='meta-row'>
							<span className='meta-key'>CREATED AT</span>
							<span className='meta-val'>{new Date(createdAt).toLocaleString()}</span>
						</div>
					</div>
				</div>
			</div>
		</div>
	);
}
