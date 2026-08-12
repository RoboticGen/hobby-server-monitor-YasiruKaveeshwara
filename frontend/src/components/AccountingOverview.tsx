/**
 * Admin accounting overview.
 *
 * Renders the host-wide capacity snapshot and the per-user allocation versus
 * quota breakdown from GET /api/accounting. The per-container history view is
 * already covered on the container detail page, so this component keeps the
 * admin dashboard focused on the bird's-eye economics.
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

function formatUsage(used: number, quota: number, unit: string): string {
	if (quota <= 0) return `${formatNumber(used)} ${unit} / unlimited`;
	return `${formatNumber(used)} / ${formatNumber(quota)} ${unit}`;
}

function formatPercent(used: number, quota: number): string {
	if (quota <= 0) return "unlimited";
	return `${((used / quota) * 100).toFixed(1)}%`;
}

function isOverQuota(used: number, quota: number): boolean {
	return quota > 0 && used > quota;
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
			<section className='accounting-panel'>
				<p>Loading accounting…</p>
			</section>
		);
	}

	if (error || !data) {
		return (
			<section className='accounting-panel'>
				<div className='panel-heading'>
					<h2>Usage accounting</h2>
				</div>
				<p className='error'>{error ?? "No accounting data available."}</p>
			</section>
		);
	}

	const host = data.host;

	return (
		<section className='accounting-panel'>
			<div className='panel-heading'>
				<h2>Usage accounting</h2>
				{data.stale && <span className='stale'>Host data stale</span>}
			</div>

			{data.stale && data.host_error && <p className='hint'>Host capacity unavailable: {data.host_error}</p>}

			<div className='summary-grid'>
				<div className='summary-card'>
					<h3>Host capacity</h3>
					{host ?
						<dl>
							<dt>RAM</dt>
							<dd>{formatUsage(data.allocated.ram_mb, host.ram_mb, "MB")}</dd>
							<dt>CPU</dt>
							<dd>{formatUsage(data.allocated.cpu, host.cpu, "cores")}</dd>
							<dt>Disk</dt>
							<dd>{formatUsage(data.allocated.disk_gb, host.disk_gb, "GB")}</dd>
							<dt>Containers</dt>
							<dd>{data.allocated.container_count}</dd>
						</dl>
					:	<p className='hint'>Host totals unavailable while LXD is offline.</p>}
				</div>

				<div className='summary-card'>
					<h3>Allocated totals</h3>
					<dl>
						<dt>RAM allocated</dt>
						<dd>{formatNumber(data.allocated.ram_mb)} MB</dd>
						<dt>CPU allocated</dt>
						<dd>{formatNumber(data.allocated.cpu)} cores</dd>
						<dt>Disk allocated</dt>
						<dd>{formatNumber(data.allocated.disk_gb)} GB</dd>
						<dt>Active containers</dt>
						<dd>{data.allocated.container_count}</dd>
					</dl>
				</div>
			</div>

			<div className='table-wrap'>
				<table>
					<thead>
						<tr>
							<th scope='col'>User</th>
							<th scope='col'>Role</th>
							<th scope='col'>Status</th>
							<th scope='col'>RAM</th>
							<th scope='col'>CPU</th>
							<th scope='col'>Disk</th>
							<th scope='col'>Containers</th>
						</tr>
					</thead>
					<tbody>
						{data.users.map((user) => {
							const overRam = isOverQuota(user.allocation.ram_mb, user.quota.ram_mb);
							const overCpu = isOverQuota(user.allocation.cpu, user.quota.cpu);
							const overDisk = isOverQuota(user.allocation.disk_gb, user.quota.disk_gb);

							return (
								<tr key={user.id} data-status={user.status}>
									<td>{user.email}</td>
									<td>{user.role}</td>
									<td>{user.status}</td>
									<td className={overRam ? "warn" : ""}>
										{formatUsage(user.allocation.ram_mb, user.quota.ram_mb, "MB")} (
										{formatPercent(user.allocation.ram_mb, user.quota.ram_mb)})
									</td>
									<td className={overCpu ? "warn" : ""}>
										{formatUsage(user.allocation.cpu, user.quota.cpu, "cores")} (
										{formatPercent(user.allocation.cpu, user.quota.cpu)})
									</td>
									<td className={overDisk ? "warn" : ""}>
										{formatUsage(user.allocation.disk_gb, user.quota.disk_gb, "GB")} (
										{formatPercent(user.allocation.disk_gb, user.quota.disk_gb)})
									</td>
									<td>{user.container_count}</td>
								</tr>
							);
						})}
					</tbody>
				</table>
			</div>

			<p className='hint'>Per-container time-window charts are available from each container's detail page.</p>
		</section>
	);
}
