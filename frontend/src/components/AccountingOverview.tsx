/**
 * Admin accounting overview.
 *
 * Renders the host-wide capacity snapshot and the per-user allocation versus
 * quota breakdown from GET /api/accounting.
 */
import { useEffect, useState } from "react";
import { apiFetch, ApiError } from "../lib/api";

interface HostCapacity {
	ram_mb: number;
	cpu: number;
	disk_gb: number;
}

interface AllocationTotals {
	ram_mb: number;
	cpu: number;
	disk_gb: number;
	container_count: number;
}

interface UserAccountingRow {
	id: string;
	email: string;
	role: string;
	status: string;
	allocation: HostCapacity;
	quota: HostCapacity;
	container_count: number;
}

interface AccountingResponse {
	host: HostCapacity | null;
	allocated: AllocationTotals;
	users: UserAccountingRow[];
	stale: boolean;
	host_error: string | null;
}

function formatNumber(value: number): string {
	return Number(value.toFixed(2)).toString();
}

function getPercent(used: number, total: number): number {
	if (!total || total <= 0) return 0;
	return Math.min(100, Math.max(0, (used / total) * 100));
}

export default function AccountingOverview() {
	const [data, setData] = useState<AccountingResponse | null>(null);
	const [error, setError] = useState<string | null>(null);
	const [loading, setLoading] = useState<boolean>(true);

	useEffect(() => {
		let cancelled = false;

		async function load(): Promise<void> {
			try {
				const response = await apiFetch<AccountingResponse>("/api/accounting");
				if (cancelled) return;
				setData(response);
				setError(null);
			} catch (err) {
				if (cancelled) return;
				setError(err instanceof ApiError ? err.message : "Could not reach the server.");
			} finally {
				if (!cancelled) setLoading(false);
			}
		}

		void load();
		const timerId = window.setInterval(load, 30_000);
		return () => {
			cancelled = true;
			window.clearInterval(timerId);
		};
	}, []);

	if (loading) {
		return (
			<section className='accounting-panel glass-card'>
				<div className='panel-loading'>
					<span className='pulse-dot'></span>
					<span>Querying host economics & capacity telemetry…</span>
				</div>
			</section>
		);
	}

	if (error || !data) {
		return (
			<section className='accounting-panel glass-card'>
				<div className='panel-heading'>
					<h2>Host Allocation & Accounting</h2>
				</div>
				<div className='alert alert-error'>{error ?? "No accounting telemetry available."}</div>
			</section>
		);
	}

	const host = data.host;
	const ramPct = host ? getPercent(data.allocated.ram_mb, host.ram_mb) : 0;
	const cpuPct = host ? getPercent(data.allocated.cpu, host.cpu) : 0;
	const diskPct = host ? getPercent(data.allocated.disk_gb, host.disk_gb) : 0;

	return (
		<section className='accounting-panel glass-card'>
			<div className='panel-heading'>
				<div className='heading-title-group'>
					<h2>Host Capacity & Economics</h2>
					<span className='containers-badge'>{data.allocated.container_count} Active Containers</span>
				</div>
				{data.stale && (
					<span className='status-badge frozen'>Host Data Stale {data.host_error ? `(${data.host_error})` : ""}</span>
				)}
			</div>

			<div className='capacity-grid'>
				{/* RAM Card */}
				<div className='stat-box'>
					<div className='stat-header'>
						<span className='stat-label'>RAM PROMISED</span>
						<span className='stat-pct mono'>{host ? `${ramPct.toFixed(1)}%` : "—"}</span>
					</div>
					<div className='stat-values'>
						<span className='stat-main mono'>{formatNumber(data.allocated.ram_mb)} MB</span>
						<span className='stat-sub mono'>/ {host ? `${formatNumber(host.ram_mb)} MB` : "offline"}</span>
					</div>
					<div className='progress-bar-container'>
						<div
							className={`progress-bar-fill ${ramPct > 90 ? "warning" : ""}`}
							style={{ width: `${Math.max(2, ramPct)}%` }}
						/>
					</div>
				</div>

				{/* CPU Card */}
				<div className='stat-box'>
					<div className='stat-header'>
						<span className='stat-label'>CPU CORES ALLOCATED</span>
						<span className='stat-pct mono'>{host ? `${cpuPct.toFixed(1)}%` : "—"}</span>
					</div>
					<div className='stat-values'>
						<span className='stat-main mono'>{formatNumber(data.allocated.cpu)} Cores</span>
						<span className='stat-sub mono'>/ {host ? `${formatNumber(host.cpu)} Cores` : "offline"}</span>
					</div>
					<div className='progress-bar-container'>
						<div
							className={`progress-bar-fill ${cpuPct > 90 ? "warning" : ""}`}
							style={{ width: `${Math.max(2, cpuPct)}%` }}
						/>
					</div>
				</div>

				{/* Disk Card */}
				<div className='stat-box'>
					<div className='stat-header'>
						<span className='stat-label'>STORAGE PROMISED</span>
						<span className='stat-pct mono'>{host ? `${diskPct.toFixed(1)}%` : "—"}</span>
					</div>
					<div className='stat-values'>
						<span className='stat-main mono'>{formatNumber(data.allocated.disk_gb)} GB</span>
						<span className='stat-sub mono'>/ {host ? `${formatNumber(host.disk_gb)} GB` : "offline"}</span>
					</div>
					<div className='progress-bar-container'>
						<div
							className={`progress-bar-fill ${diskPct > 90 ? "warning" : ""}`}
							style={{ width: `${Math.max(2, diskPct)}%` }}
						/>
					</div>
				</div>
			</div>
		</section>
	);
}
