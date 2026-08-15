/**
 * Universal Confirmation & Alert Modal Dialog Component.
 *
 * Listens to global 'hsm:confirm' events and renders an accessible, focus-trapped
 * confirmation modal with Danger/Primary variants, loading states, and keyboard bindings.
 */
import { useEffect, useRef, useState } from "react";
import type { ConfirmDialogOptions } from "../lib/alerts";

export default function ConfirmModal() {
	const [options, setOptions] = useState<ConfirmDialogOptions | null>(null);
	const [busy, setBusy] = useState(false);
	const confirmBtnRef = useRef<HTMLButtonElement | null>(null);

	useEffect(() => {
		const handleConfirmEvent = (event: Event) => {
			const customEvent = event as CustomEvent<ConfirmDialogOptions>;
			if (!customEvent.detail) return;
			setBusy(false);
			setOptions(customEvent.detail);
		};

		window.addEventListener("hsm:confirm", handleConfirmEvent);
		return () => window.removeEventListener("hsm:confirm", handleConfirmEvent);
	}, []);

	useEffect(() => {
		if (!options) return;

		// Focus the confirm or cancel button automatically
		const timer = setTimeout(() => {
			confirmBtnRef.current?.focus();
		}, 50);

		// Handle Escape key to cancel
		const handleKeyDown = (e: KeyboardEvent) => {
			if (e.key === "Escape" && !busy) {
				handleCancel();
			}
		};

		window.addEventListener("keydown", handleKeyDown);
		return () => {
			clearTimeout(timer);
			window.removeEventListener("keydown", handleKeyDown);
		};
	}, [options, busy]);

	const handleCancel = () => {
		if (!options || busy) return;
		options.onCancel?.();
		setOptions(null);
	};

	const handleConfirm = async () => {
		if (!options || busy) return;
		setBusy(true);
		try {
			await options.onConfirm();
		} finally {
			setBusy(false);
			setOptions(null);
		}
	};

	if (!options) {
		return null;
	}

	const isDanger = options.isDanger ?? true;
	const iconType = options.iconType || (isDanger ? "danger" : "info");
	const confirmText = options.confirmText || (isDanger ? "Confirm Action" : "Proceed");
	const cancelText = options.cancelText !== undefined ? options.cancelText : "Cancel";

	return (
		<div
			className='hsm-modal-backdrop'
			role='dialog'
			aria-modal='true'
			aria-labelledby='hsm-confirm-title'
			onClick={(e) => {
				if (e.target === e.currentTarget && !busy) {
					handleCancel();
				}
			}}>
			<div className='hsm-confirm-card'>
				<div className='hsm-confirm-header'>
					<div className={`hsm-confirm-icon-badge ${iconType}`}>
						{iconType === "danger" && (
							<svg width='22' height='22' viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2.5'>
								<circle cx='12' cy='12' r='10'></circle>
								<line x1='12' y1='8' x2='12' y2='12'></line>
								<line x1='12' y1='16' x2='12.01' y2='16'></line>
							</svg>
						)}
						{iconType === "warning" && (
							<svg width='22' height='22' viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2.5'>
								<path d='M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z'></path>
								<line x1='12' y1='9' x2='12' y2='13'></line>
								<line x1='12' y1='17' x2='12.01' y2='17'></line>
							</svg>
						)}
						{(iconType === "info" || iconType === "question") && (
							<svg width='22' height='22' viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2.5'>
								<circle cx='12' cy='12' r='10'></circle>
								<line x1='12' y1='16' x2='12' y2='12'></line>
								<line x1='12' y1='8' x2='12.01' y2='8'></line>
							</svg>
						)}
					</div>
					<div>
						<h3 id='hsm-confirm-title' className='hsm-confirm-title'>
							{options.title}
						</h3>
					</div>
				</div>

				<div className='hsm-confirm-body'>
					<p>{options.message}</p>
				</div>

				<div className='hsm-confirm-footer'>
					{cancelText && (
						<button type='button' className='btn-modal-cancel' disabled={busy} onClick={handleCancel}>
							{cancelText}
						</button>
					)}
					<button
						ref={confirmBtnRef}
						type='button'
						className={`btn-modal-confirm ${isDanger ? "danger" : "primary"}`}
						disabled={busy}
						onClick={() => void handleConfirm()}>
						{busy && (
							<svg
								width='16'
								height='16'
								viewBox='0 0 24 24'
								fill='none'
								stroke='currentColor'
								strokeWidth='2.5'
								className='btn-spinner'
								style={{ animation: "spin 1s linear infinite" }}>
								<circle cx='12' cy='12' r='10' strokeOpacity='0.25'></circle>
								<path d='M12 2a10 10 0 0 1 10 10'></path>
							</svg>
						)}
						<span>{busy ? "Processing…" : confirmText}</span>
					</button>
				</div>
			</div>
		</div>
	);
}
