/**
 * Unified, organized User Management Modal Dialog.
 *
 * Provides a clean, tabbed popup experience:
 * - Tab 1: Account & Role Privileges (Role, Status, Revocation)
 * - Tab 2: Resource Quota Ceilings (RAM, CPU, Disk with allocation gauges)
 * - Tab 3: Container Instance Permissions (Assignment toggles per container)
 */
import { useEffect, useState, type SyntheticEvent } from "react";
import { apiFetch, ApiError } from "../lib/api";
import { toast, showConfirm } from "../lib/alerts";

export type UserRole = "admin" | "user";
export type UserStatus = "invited" | "active" | "revoked";

export interface QuotaAllocation {
	ram_mb: number;
	cpu: number;
	disk_gb: number;
}

export interface UserRecord {
	id: string;
	email: string;
	role: string;
	status: string;
	quota_ram_mb: number;
	quota_cpu: number;
	quota_disk_gb: number;
	allocation: QuotaAllocation;
}

export interface ContainerRecord {
	id: string;
	lxd_name: string;
}

export interface UserManageModalProps {
	user: UserRecord;
	allContainers: ContainerRecord[];
	assignmentStates: Map<string, "unknown" | "granted" | "revoked">;
	onClose: () => void;
	onUserUpdated: () => void;
}

type ActiveTab = "account" | "quotas" | "assignments";

function formatNumber(value: number): string {
	return String(Number(value.toFixed(2)));
}

export default function UserManageModal({
	user,
	allContainers,
	assignmentStates,
	onClose,
	onUserUpdated,
}: UserManageModalProps) {
	const [activeTab, setActiveTab] = useState<ActiveTab>("account");

	// Account Tab State
	const [role, setRole] = useState<UserRole>(user.role as UserRole);
	const [status, setStatus] = useState<UserStatus>(user.status as UserStatus);
	const [accountSaving, setAccountSaving] = useState(false);
	const [accountNotice, setAccountNotice] = useState<{ type: "success" | "error"; text: string } | null>(null);

	// Quota Tab State
	const [ramMb, setRamMb] = useState<number>(user.quota_ram_mb);
	const [cpu, setCpu] = useState<number>(user.quota_cpu);
	const [diskGb, setDiskGb] = useState<number>(user.quota_disk_gb);
	const [quotaSaving, setQuotaSaving] = useState(false);
	const [quotaNotice, setQuotaNotice] = useState<{ type: "success" | "error"; text: string } | null>(null);

	// Assignments State
	const [busyContainerId, setBusyContainerId] = useState<string | null>(null);
	const [assignmentNotice, setAssignmentNotice] = useState<{ type: "success" | "error"; text: string } | null>(null);
	const [containerFilter, setContainerFilter] = useState("");

	// Track keyboard Escape key
	useEffect(() => {
		const handleKeyDown = (e: KeyboardEvent) => {
			if (e.key === "Escape") onClose();
		};
		window.addEventListener("keydown", handleKeyDown);
		return () => window.removeEventListener("keydown", handleKeyDown);
	}, [onClose]);

	// Account Form Submission
	const handleAccountSubmit = async (e: SyntheticEvent<HTMLFormElement>) => {
		e.preventDefault();
		setAccountSaving(true);
		setAccountNotice(null);
		toast.info("Saving account privileges…");

		try {
			await apiFetch(`/api/users/${encodeURIComponent(user.id)}`, {
				method: "PATCH",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ role, status }),
			});

			const msg = "Account settings updated successfully.";
			setAccountNotice({ type: "success", text: msg });
			toast.success(msg, "Account Saved");
			onUserUpdated();
		} catch (err) {
			const errMsg = err instanceof ApiError ? `${err.status}: ${err.message}` : "Failed to update account.";
			setAccountNotice({
				type: "error",
				text: errMsg,
			});
			toast.error(err, "Failed to Update Account");
		} finally {
			setAccountSaving(false);
		}
	};

	// Revoke Account Shortcut
	const handleRevokeAccount = async () => {
		await showConfirm({
			title: "Revoke User Access",
			message: `Revoke account access for ${user.email}? They will no longer be able to authenticate and active sessions will be terminated immediately.`,
			confirmText: "Revoke Access",
			cancelText: "Cancel",
			isDanger: true,
			iconType: "danger",
			onConfirm: async () => {
				setAccountSaving(true);
				setAccountNotice(null);

				try {
					await apiFetch(`/api/users/${encodeURIComponent(user.id)}`, {
						method: "PATCH",
						headers: { "Content-Type": "application/json" },
						body: JSON.stringify({ status: "revoked" }),
					});
					setStatus("revoked");
					const msg = `${user.email} access revoked.`;
					setAccountNotice({ type: "success", text: msg });
					toast.success(msg, "User Revoked");
					onUserUpdated();
				} catch (err) {
					const errMsg = err instanceof ApiError ? err.message : "Could not revoke account.";
					setAccountNotice({
						type: "error",
						text: errMsg,
					});
					toast.error(err, "Failed to Revoke User");
				} finally {
					setAccountSaving(false);
				}
			},
		});
	};

	// Quota Form Submission
	const handleQuotaSubmit = async (e: SyntheticEvent<HTMLFormElement>) => {
		e.preventDefault();
		setQuotaSaving(true);
		setQuotaNotice(null);
		toast.info("Saving quota limits…");

		try {
			await apiFetch(`/api/users/${encodeURIComponent(user.id)}`, {
				method: "PATCH",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					quota_ram_mb: ramMb,
					quota_cpu: cpu,
					quota_disk_gb: diskGb,
				}),
			});

			const msg = "Resource quotas updated successfully.";
			setQuotaNotice({ type: "success", text: msg });
			toast.success(msg, "Quotas Saved");
			onUserUpdated();
		} catch (err) {
			const errMsg = err instanceof ApiError ? `${err.status}: ${err.message}` : "Failed to update quotas.";
			setQuotaNotice({
				type: "error",
				text: errMsg,
			});
			toast.error(err, "Failed to Save Quotas");
		} finally {
			setQuotaSaving(false);
		}
	};

	// Container Assignment Toggle
	const handleToggleAssignment = async (containerId: string, containerName: string, grant: boolean) => {
		setBusyContainerId(containerId);
		setAssignmentNotice(null);

		const path = `/api/users/${encodeURIComponent(user.id)}/containers/${encodeURIComponent(containerId)}`;
		const key = `${user.id}:${containerId}`;

		try {
			if (grant) {
				await apiFetch(path, { method: "POST" });
				assignmentStates.set(key, "granted");
				const msg = `Granted access to ${containerName}.`;
				setAssignmentNotice({ type: "success", text: msg });
				toast.success(msg, "Container Assigned");
			} else {
				await apiFetch(path, { method: "DELETE" });
				assignmentStates.set(key, "revoked");
				const msg = `Revoked access to ${containerName}.`;
				setAssignmentNotice({ type: "success", text: msg });
				toast.success(msg, "Container Revoked");
			}
			onUserUpdated();
		} catch (err) {
			const errMsg = err instanceof ApiError ? err.message : "Failed to update container assignment.";
			setAssignmentNotice({
				type: "error",
				text: errMsg,
			});
			toast.error(err, "Assignment Update Failed");
		} finally {
			setBusyContainerId(null);
		}
	};

	const normStatus = status.toLowerCase();
	const badgeClass =
		normStatus === "active" ? "running"
		: normStatus === "invited" ? "frozen"
		: "stopped";

	const filteredContainers = allContainers.filter((c) =>
		c.lxd_name.toLowerCase().includes(containerFilter.toLowerCase()),
	);

	const assignedCount = allContainers.filter((c) => assignmentStates.get(`${user.id}:${c.id}`) === "granted").length;

	const ramPct = ramMb > 0 ? Math.min(100, (user.allocation.ram_mb / ramMb) * 100) : 0;
	const cpuPct = cpu > 0 ? Math.min(100, (user.allocation.cpu / cpu) * 100) : 0;
	const diskPct = diskGb > 0 ? Math.min(100, (user.allocation.disk_gb / diskGb) * 100) : 0;

	return (
		<div className='modal-backdrop' onClick={(e) => e.target === e.currentTarget && onClose()}>
			<div className='modal-window glass-card' role='dialog' aria-modal='true'>
				{/* 1. Modal Top Bar */}
				<div className='modal-header'>
					<div className='modal-user-info'>
						<span className='user-avatar-badge'>👤</span>
						<div>
							<h3 className='modal-title'>{user.email}</h3>
							<div className='modal-sub-tags'>
								<span className={`role-pill ${role}`}>{role}</span>
								<span className={`status-badge ${badgeClass}`}>{status}</span>
								<span className='meta-tag mono'>ID: {user.id}</span>
							</div>
						</div>
					</div>
					<button
						type='button'
						className='modal-close-button'
						onClick={onClose}
						aria-label='Close user settings'
						title='Close (Esc)'>
						<svg width='18' height='18' viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2.2'>
							<line x1='18' y1='6' x2='6' y2='18' />
							<line x1='6' y1='6' x2='18' y2='18' />
						</svg>
					</button>
				</div>

				{/* 2. Modal Tab Navigation */}
				<div className='modal-tab-bar'>
					<button
						type='button'
						className={`modal-tab-btn ${activeTab === "account" ? "active" : ""}`}
						onClick={() => setActiveTab("account")}>
						<span>👤 Account & Role</span>
					</button>
					<button
						type='button'
						className={`modal-tab-btn ${activeTab === "quotas" ? "active" : ""}`}
						onClick={() => setActiveTab("quotas")}>
						<span>📊 Resource Quotas</span>
					</button>
					<button
						type='button'
						className={`modal-tab-btn ${activeTab === "assignments" ? "active" : ""}`}
						onClick={() => setActiveTab("assignments")}>
						<span>📦 Container Access ({assignedCount})</span>
					</button>
				</div>

				{/* 3. Modal Tab Body */}
				<div className='modal-body'>
					{/* TAB 1: Account & Role Privileges */}
					{activeTab === "account" && (
						<form className='modal-tab-pane' onSubmit={(e) => void handleAccountSubmit(e)}>
							{accountNotice && (
								<div className={`alert ${accountNotice.type === "success" ? "alert-success" : "alert-error"}`}>
									{accountNotice.text}
								</div>
							)}

							<div className='modal-section-box'>
								<h4>System Access Permissions</h4>
								<p className='section-desc'>Configure authorization level and account activation state.</p>

								<div className='form-grid-2col'>
									<div className='form-group'>
										<label htmlFor={`modal-role-${user.id}`}>System Role</label>
										<select
											id={`modal-role-${user.id}`}
											value={role}
											onChange={(e) => setRole(e.target.value as UserRole)}
											disabled={accountSaving}>
											<option value='user'>Regular User (Restricted to Quotas)</option>
											<option value='admin'>System Administrator (Full Access)</option>
										</select>
										<span className='field-hint'>Admins can provision containers and manage other users.</span>
									</div>

									<div className='form-group'>
										<label htmlFor={`modal-status-${user.id}`}>Account Status</label>
										<select
											id={`modal-status-${user.id}`}
											value={status}
											onChange={(e) => setStatus(e.target.value as UserStatus)}
											disabled={accountSaving}>
											<option value='active'>Active (Access Permitted)</option>
											<option value='invited'>Invited (Pending First Sign-in)</option>
											<option value='revoked'>Revoked (Access Blocked)</option>
										</select>
										<span className='field-hint'>Revoked users cannot sign in or execute commands.</span>
									</div>
								</div>
							</div>

							<div className='modal-action-footer'>
								{status !== "revoked" && (
									<button
										type='button'
										className='btn danger'
										onClick={() => void handleRevokeAccount()}
										disabled={accountSaving}>
										Revoke Account
									</button>
								)}
								<button
									type='submit'
									className='btn btn-primary'
									disabled={accountSaving}
									style={{ marginLeft: "auto" }}>
									{accountSaving ? "Saving…" : "Save Account Settings"}
								</button>
							</div>
						</form>
					)}

					{/* TAB 2: Resource Quotas */}
					{activeTab === "quotas" && (
						<form className='modal-tab-pane' onSubmit={(e) => void handleQuotaSubmit(e)}>
							{quotaNotice && (
								<div className={`alert ${quotaNotice.type === "success" ? "alert-success" : "alert-error"}`}>
									{quotaNotice.text}
								</div>
							)}

							{/* Allocation Usage Overview Gauges */}
							<div className='quota-overview-cards'>
								<div className='quota-mini-gauge'>
									<div className='gauge-label-row'>
										<span>RAM ALLOCATED</span>
										<span className='mono'>
											{formatNumber(user.allocation.ram_mb)} / {ramMb === 0 ? "∞" : `${ramMb} MB`}
										</span>
									</div>
									<div className='progress-bar-container'>
										<div className='progress-bar-fill' style={{ width: `${ramPct}%` }} />
									</div>
								</div>

								<div className='quota-mini-gauge'>
									<div className='gauge-label-row'>
										<span>CPU CORES</span>
										<span className='mono'>
											{formatNumber(user.allocation.cpu)} / {cpu === 0 ? "∞" : `${cpu} Cores`}
										</span>
									</div>
									<div className='progress-bar-container'>
										<div className='progress-bar-fill' style={{ width: `${cpuPct}%` }} />
									</div>
								</div>

								<div className='quota-mini-gauge'>
									<div className='gauge-label-row'>
										<span>DISK STORAGE</span>
										<span className='mono'>
											{formatNumber(user.allocation.disk_gb)} / {diskGb === 0 ? "∞" : `${diskGb} GB`}
										</span>
									</div>
									<div className='progress-bar-container'>
										<div className='progress-bar-fill' style={{ width: `${diskPct}%` }} />
									</div>
								</div>
							</div>

							<div className='modal-section-box' style={{ marginTop: "1rem" }}>
								<h4>Resource Limit Ceilings</h4>
								<p className='section-desc'>Set hard capacity thresholds for this account. Set to 0 for unlimited.</p>

								<div className='form-grid-3col'>
									<div className='form-group'>
										<label htmlFor={`quota-ram-${user.id}`}>RAM Limit (MB)</label>
										<input
											id={`quota-ram-${user.id}`}
											type='number'
											min={0}
											step={256}
											value={ramMb}
											onChange={(e) => setRamMb(Number(e.target.value))}
											disabled={quotaSaving}
										/>
										<span className='field-hint'>0 = Unlimited</span>
									</div>

									<div className='form-group'>
										<label htmlFor={`quota-cpu-${user.id}`}>CPU Limit (Cores)</label>
										<input
											id={`quota-cpu-${user.id}`}
											type='number'
											min={0}
											step={0.5}
											value={cpu}
											onChange={(e) => setCpu(Number(e.target.value))}
											disabled={quotaSaving}
										/>
										<span className='field-hint'>0 = Unlimited</span>
									</div>

									<div className='form-group'>
										<label htmlFor={`quota-disk-${user.id}`}>Disk Limit (GB)</label>
										<input
											id={`quota-disk-${user.id}`}
											type='number'
											min={0}
											step={1}
											value={diskGb}
											onChange={(e) => setDiskGb(Number(e.target.value))}
											disabled={quotaSaving}
										/>
										<span className='field-hint'>0 = Unlimited</span>
									</div>
								</div>
							</div>

							<div className='modal-action-footer'>
								<button type='submit' className='btn btn-primary' disabled={quotaSaving} style={{ marginLeft: "auto" }}>
									{quotaSaving ? "Applying…" : "Apply Quota Limits"}
								</button>
							</div>
						</form>
					)}

					{/* TAB 3: Container Access Assignments */}
					{activeTab === "assignments" && (
						<div className='modal-tab-pane'>
							{assignmentNotice && (
								<div className={`alert ${assignmentNotice.type === "success" ? "alert-success" : "alert-error"}`}>
									{assignmentNotice.text}
								</div>
							)}

							<div className='assignments-filter-row'>
								<div className='search-input-wrap'>
									<span className='search-icon'>🔍</span>
									<input
										type='search'
										placeholder='Search host containers…'
										value={containerFilter}
										onChange={(e) => setContainerFilter(e.target.value)}
									/>
								</div>
								<span className='assignment-summary-pill'>
									{assignedCount} of {allContainers.length} Assigned
								</span>
							</div>

							{filteredContainers.length === 0 ?
								<div className='empty-state glass-card' style={{ padding: "2rem", margin: "1rem 0" }}>
									<div className='empty-icon'>📦</div>
									<h4>No Matching Containers</h4>
									<p>No container instances match the search term.</p>
								</div>
							:	<div className='modal-assignments-grid'>
									{filteredContainers.map((container) => {
										const key = `${user.id}:${container.id}`;
										const current = assignmentStates.get(key) ?? "unknown";
										const isGranted = current === "granted";
										const isBusy = busyContainerId === container.id;

										return (
											<div key={container.id} className={`modal-container-card ${isGranted ? "assigned" : ""}`}>
												<div className='container-card-info'>
													<span className='container-glyph'>📦</span>
													<div className='container-text-block'>
														<span className='container-title-name'>{container.lxd_name}</span>
														<span className={`status-badge ${isGranted ? "running" : "stopped"}`}>
															{isGranted ? "Access Granted" : "Restricted"}
														</span>
													</div>
												</div>

												<div className='container-card-action'>
													{isGranted ?
														<button
															type='button'
															className='btn btn-sm danger'
															onClick={() => void handleToggleAssignment(container.id, container.lxd_name, false)}
															disabled={isBusy}>
															{isBusy ? "…" : "Revoke Access"}
														</button>
													:	<button
															type='button'
															className='btn btn-sm btn-primary'
															onClick={() => void handleToggleAssignment(container.id, container.lxd_name, true)}
															disabled={isBusy}>
															{isBusy ? "…" : "Grant Access"}
														</button>
													}
												</div>
											</div>
										);
									})}
								</div>
							}
						</div>
					)}
				</div>
			</div>
		</div>
	);
}
