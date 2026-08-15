/**
 * Organized, User-Friendly User Invitation & Authorization Form (Admin Only).
 *
 * Features:
 * - Clean 2-column layout with visual role selection
 * - 1-Click Quota Profile Presets (Lightweight, Standard, Power User, Unlimited)
 * - Custom Quota fine-tuning accordion
 * - Clear field hints and instant validation feedback
 */
import { useState, type SyntheticEvent } from "react";
import { apiFetch, ApiError } from "../lib/api";
import { toast } from "../lib/alerts";

type UserRole = "admin" | "user";

interface InvitedUser {
	id: string;
	email: string;
	role: UserRole;
	status: string;
}

export interface InviteUserFormProps {
	onInvited?: () => void;
}

interface QuotaPreset {
	id: string;
	label: string;
	badge: string;
	ramMb: number;
	cpu: number;
	diskGb: number;
	desc: string;
}

const PRESETS: QuotaPreset[] = [
	{
		id: "light",
		label: "Lightweight",
		badge: "1 GB · 1 CPU · 10 GB",
		ramMb: 1024,
		cpu: 1,
		diskGb: 10,
		desc: "For small microservices and scripts",
	},
	{
		id: "standard",
		label: "Standard",
		badge: "2 GB · 2 CPU · 20 GB",
		ramMb: 2048,
		cpu: 2,
		diskGb: 20,
		desc: "Recommended for general development",
	},
	{
		id: "power",
		label: "Power User",
		badge: "4 GB · 4 CPU · 40 GB",
		ramMb: 4096,
		cpu: 4,
		diskGb: 40,
		desc: "High capacity for heavy builds & databases",
	},
	{
		id: "unlimited",
		label: "Unlimited",
		badge: "∞ No Hard Caps",
		ramMb: 0,
		cpu: 0,
		diskGb: 0,
		desc: "Unrestricted host capacity allocation",
	},
];

export default function InviteUserForm({ onInvited }: InviteUserFormProps) {
	const [email, setEmail] = useState<string>("");
	const [role, setRole] = useState<UserRole>("user");
	const [selectedPreset, setSelectedPreset] = useState<string>("standard");

	const [quotaRamMb, setQuotaRamMb] = useState<number>(2048);
	const [quotaCpu, setQuotaCpu] = useState<number>(2);
	const [quotaDiskGb, setQuotaDiskGb] = useState<number>(20);
	const [showCustomLimits, setShowCustomLimits] = useState<boolean>(false);

	const [submitting, setSubmitting] = useState<boolean>(false);
	const [error, setError] = useState<string | null>(null);
	const [success, setSuccess] = useState<string | null>(null);

	const handlePresetSelect = (preset: QuotaPreset) => {
		setSelectedPreset(preset.id);
		setQuotaRamMb(preset.ramMb);
		setQuotaCpu(preset.cpu);
		setQuotaDiskGb(preset.diskGb);
	};

	async function handleSubmit(event: SyntheticEvent<HTMLFormElement>) {
		event.preventDefault();

		if (!email.trim() || !email.includes("@")) {
			toast.warning("Please enter a valid Google account email address.");
			return;
		}

		setSubmitting(true);
		setError(null);
		setSuccess(null);
		toast.info(`Authorizing access for ${email.trim()}…`);

		try {
			const invited = await apiFetch<InvitedUser>("/api/users", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					email: email.trim(),
					role,
					quota_ram_mb: quotaRamMb,
					quota_cpu: quotaCpu,
					quota_disk_gb: quotaDiskGb,
				}),
			});

			const successMsg = `Successfully authorized ${invited.email} (${invited.role.toUpperCase()}). The user can now authenticate instantly using Google OAuth.`;
			setSuccess(successMsg);
			toast.success(`User ${invited.email} authorized as ${invited.role.toUpperCase()}.`, "User Authorized");
			setEmail("");
			setSelectedPreset("standard");
			setQuotaRamMb(2048);
			setQuotaCpu(2);
			setQuotaDiskGb(20);
			setShowCustomLimits(false);
			onInvited?.();
		} catch (err) {
			const errorMsg =
				err instanceof ApiError ? `${err.status}: ${err.message}` : "Could not reach the server to authorize user.";
			setError(errorMsg);
			toast.error(err, "Authorization Failed");
		} finally {
			setSubmitting(false);
		}
	}

	return (
		<section className='invite-form-section glass-card'>
			{/* Section Header */}
			<div className='invite-card-header'>
				<div className='invite-header-left'>
					<div className='invite-badge-icon'>
						<svg width='22' height='22' viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2'>
							<path d='M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2'></path>
							<circle cx='8.5' cy='7' r='4'></circle>
							<line x1='20' y1='8' x2='20' y2='14'></line>
							<line x1='23' y1='11' x2='17' y2='11'></line>
						</svg>
					</div>
					<div>
						<h2 className='invite-title'>Authorize & Provision User Access</h2>
						<p className='section-desc'>Grant Google OAuth sign-in authorization with tailored resource quotas.</p>
					</div>
				</div>
			</div>

			{/* Feedback Banners */}
			{error && (
				<div className='alert alert-error' style={{ margin: "1rem 1.5rem 0" }}>
					<span>⚠️ {error}</span>
				</div>
			)}
			{success && (
				<div className='alert alert-success' style={{ margin: "1rem 1.5rem 0" }}>
					<span>✓ {success}</span>
				</div>
			)}

			<form className='invite-main-form' onSubmit={(e) => void handleSubmit(e)}>
				<div className='invite-form-grid'>
					{/* LEFT COLUMN: Identity & Role Selection */}
					<div className='invite-col-left'>
						<div className='form-group'>
							<label htmlFor='invite-email' className='form-label-bold'>
								Google Account Email <span className='required-star'>*</span>
							</label>
							<div className='input-with-glyph'>
								<span className='input-glyph'>✉️</span>
								<input
									id='invite-email'
									type='email'
									value={email}
									onChange={(e) => setEmail(e.target.value)}
									placeholder='developer@organization.com'
									required
									disabled={submitting}
								/>
							</div>
							<span className='field-hint'>User will authenticate with this exact Google address.</span>
						</div>

						<div className='form-group' style={{ marginTop: "1.25rem" }}>
							<label className='form-label-bold'>System Authorization Role</label>
							<div className='role-selection-grid'>
								<label
									className={`role-option-card ${role === "user" ? "selected" : ""}`}
									onClick={() => setRole("user")}>
									<input
										type='radio'
										name='user-role'
										value='user'
										checked={role === "user"}
										onChange={() => setRole("user")}
										disabled={submitting}
									/>
									<div className='role-card-content'>
										<span className='role-card-title'>Regular User</span>
										<span className='role-card-desc'>
											Restricted to personal quota and specifically assigned containers.
										</span>
									</div>
								</label>

								<label
									className={`role-option-card ${role === "admin" ? "selected" : ""}`}
									onClick={() => setRole("admin")}>
									<input
										type='radio'
										name='user-role'
										value='admin'
										checked={role === "admin"}
										onChange={() => setRole("admin")}
										disabled={submitting}
									/>
									<div className='role-card-content'>
										<span className='role-card-title'>Administrator</span>
										<span className='role-card-desc'>
											Full host control, container creation, user management & terminal.
										</span>
									</div>
								</label>
							</div>
						</div>
					</div>

					{/* RIGHT COLUMN: Resource Quota Profiles */}
					<div className='invite-col-right'>
						<div className='form-group'>
							<div className='preset-header-row'>
								<label className='form-label-bold'>Initial Resource Quota Profile</label>
								<button
									type='button'
									className='preset-toggle-btn'
									onClick={() => setShowCustomLimits(!showCustomLimits)}>
									{showCustomLimits ? "Use Profile Presets" : "⚙️ Customize Limits"}
								</button>
							</div>

							{!showCustomLimits ?
								<div className='presets-grid'>
									{PRESETS.map((preset) => {
										const isSelected = selectedPreset === preset.id;
										return (
											<div
												key={preset.id}
												className={`preset-card ${isSelected ? "selected" : ""}`}
												onClick={() => handlePresetSelect(preset)}>
												<div className='preset-title-row'>
													<span className='preset-name'>{preset.label}</span>
													<span className='preset-badge-tag'>{preset.badge}</span>
												</div>
												<p className='preset-desc'>{preset.desc}</p>
											</div>
										);
									})}
								</div>
							:	<div className='custom-quota-panel'>
									<div className='custom-inputs-row'>
										<div className='form-group'>
											<label htmlFor='invite-ram'>RAM (MB)</label>
											<input
												id='invite-ram'
												type='number'
												min={0}
												step={256}
												value={quotaRamMb}
												onChange={(e) => {
													setQuotaRamMb(Number(e.target.value));
													setSelectedPreset("custom");
												}}
												disabled={submitting}
											/>
											<span className='field-hint'>0 = Unlimited</span>
										</div>

										<div className='form-group'>
											<label htmlFor='invite-cpu'>CPU (Cores)</label>
											<input
												id='invite-cpu'
												type='number'
												min={0}
												step={0.5}
												value={quotaCpu}
												onChange={(e) => {
													setQuotaCpu(Number(e.target.value));
													setSelectedPreset("custom");
												}}
												disabled={submitting}
											/>
											<span className='field-hint'>0 = Unlimited</span>
										</div>

										<div className='form-group'>
											<label htmlFor='invite-disk'>Disk (GB)</label>
											<input
												id='invite-disk'
												type='number'
												min={0}
												step={1}
												value={quotaDiskGb}
												onChange={(e) => {
													setQuotaDiskGb(Number(e.target.value));
													setSelectedPreset("custom");
												}}
												disabled={submitting}
											/>
											<span className='field-hint'>0 = Unlimited</span>
										</div>
									</div>
								</div>
							}
						</div>

						{/* Quota Summary Card */}
						<div className='quota-summary-strip'>
							<span className='summary-label'>ALLOCATION CEILING:</span>
							<span className='summary-val-pill'>{quotaRamMb === 0 ? "∞ RAM" : `${quotaRamMb} MB RAM`}</span>
							<span className='summary-val-pill'>{quotaCpu === 0 ? "∞ CPU" : `${quotaCpu} Cores`}</span>
							<span className='summary-val-pill'>{quotaDiskGb === 0 ? "∞ Disk" : `${quotaDiskGb} GB Disk`}</span>
						</div>
					</div>
				</div>

				{/* Form Footer Action */}
				<div className='invite-form-footer'>
					<button type='submit' className='btn btn-primary invite-submit-btn' disabled={submitting || !email.trim()}>
						{submitting ?
							<>
								<span className='cyber-spinner' style={{ width: 16, height: 16 }} />
								<span>Authorizing Account…</span>
							</>
						:	<>
								<span>Authorize & Provision User</span>
								<span className='btn-arrow'>→</span>
							</>
						}
					</button>
				</div>
			</form>
		</section>
	);
}
