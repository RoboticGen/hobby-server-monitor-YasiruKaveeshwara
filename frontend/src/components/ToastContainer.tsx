/**
 * Floating Toast Notification Container Component.
 *
 * Listens to global 'hsm:toast' and 'hsm:toast:dismiss' events, managing
 * auto-dismiss timers, animated progress indicators, and manual dismissals.
 */
import { useEffect, useState } from "react";
import type { ToastItem } from "../lib/alerts";

export default function ToastContainer() {
	const [toasts, setToasts] = useState<ToastItem[]>([]);
	const [exitingIds, setExitingIds] = useState<Set<string>>(new Set());

	useEffect(() => {
		const handleAddToast = (event: Event) => {
			const customEvent = event as CustomEvent<ToastItem>;
			if (!customEvent.detail) return;

			const newToast = customEvent.detail;
			setToasts((prev) => [newToast, ...prev.slice(0, 4)]); // Keep maximum 5 toasts

			if (newToast.duration > 0) {
				window.setTimeout(() => {
					dismissToast(newToast.id);
				}, newToast.duration);
			}
		};

		const handleDismissToast = (event: Event) => {
			const customEvent = event as CustomEvent<{ id: string }>;
			if (customEvent.detail?.id) {
				dismissToast(customEvent.detail.id);
			}
		};

		window.addEventListener("hsm:toast", handleAddToast);
		window.addEventListener("hsm:toast:dismiss", handleDismissToast);

		return () => {
			window.removeEventListener("hsm:toast", handleAddToast);
			window.removeEventListener("hsm:toast:dismiss", handleDismissToast);
		};
	}, []);

	const dismissToast = (id: string) => {
		setExitingIds((prev) => new Set(prev).add(id));
		window.setTimeout(() => {
			setToasts((prev) => prev.filter((t) => t.id !== id));
			setExitingIds((prev) => {
				const next = new Set(prev);
				next.delete(id);
				return next;
			});
		}, 200);
	};

	if (toasts.length === 0) {
		return null;
	}

	return (
		<div className='hsm-toast-viewport' role='region' aria-live='polite' aria-label='Notifications'>
			{toasts.map((item) => {
				const isExiting = exitingIds.has(item.id);
				return (
					<div key={item.id} className={`hsm-toast-item toast-${item.type} ${isExiting ? "exiting" : ""}`} role='alert'>
						<div className='hsm-toast-body'>
							<div className='hsm-toast-icon-wrap'>
								{item.type === "success" && (
									<svg width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2.5'>
										<path d='M22 11.08V12a10 10 0 1 1-5.93-9.14'></path>
										<polyline points='22 4 12 14.01 9 11.01'></polyline>
									</svg>
								)}
								{item.type === "error" && (
									<svg width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2.5'>
										<circle cx='12' cy='12' r='10'></circle>
										<line x1='15' y1='9' x2='9' y2='15'></line>
										<line x1='9' y1='9' x2='15' y2='15'></line>
									</svg>
								)}
								{item.type === "warning" && (
									<svg width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2.5'>
										<path d='M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z'></path>
										<line x1='12' y1='9' x2='12' y2='13'></line>
										<line x1='12' y1='17' x2='12.01' y2='17'></line>
									</svg>
								)}
								{item.type === "info" && (
									<svg width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2.5'>
										<circle cx='12' cy='12' r='10'></circle>
										<line x1='12' y1='16' x2='12' y2='12'></line>
										<line x1='12' y1='8' x2='12.01' y2='8'></line>
									</svg>
								)}
							</div>

							<div className='hsm-toast-content'>
								{item.title && <div className='hsm-toast-title'>{item.title}</div>}
								<div className='hsm-toast-msg'>{item.message}</div>
								{item.action && (
									<button
										type='button'
										className='hsm-toast-action-btn'
										onClick={() => {
											item.action?.onClick();
											dismissToast(item.id);
										}}>
										{item.action.label}
									</button>
								)}
							</div>

							<button
								type='button'
								className='hsm-toast-close-btn'
								aria-label='Dismiss notification'
								onClick={() => dismissToast(item.id)}>
								<svg width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2'>
									<line x1='18' y1='6' x2='6' y2='18'></line>
									<line x1='6' y1='6' x2='18' y2='18'></line>
								</svg>
							</button>
						</div>

						{item.duration > 0 && (
							<div className='hsm-toast-progress-track'>
								<div className='hsm-toast-progress-bar' style={{ animationDuration: `${item.duration}ms` }}></div>
							</div>
						)}
					</div>
				);
			})}
		</div>
	);
}
