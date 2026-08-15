/**
 * Unified Alert, Toast Notification, and Confirmation Dialog Dispatcher.
 *
 * Provides a lightweight, event-driven notification architecture that works
 * universally across both React components (islands) and Astro client scripts.
 */
import { ApiError } from "./api";

export type ToastType = "success" | "error" | "warning" | "info";

export interface ToastAction {
	label: string;
	onClick: () => void;
}

export interface ToastItem {
	id: string;
	type: ToastType;
	title?: string;
	message: string;
	duration: number; // in milliseconds (0 = persistent until dismissed)
	action?: ToastAction;
	createdAt: number;
}

export interface ToastOptions {
	title?: string;
	message: string;
	type?: ToastType;
	duration?: number;
	action?: ToastAction;
}

export interface ConfirmDialogOptions {
	id?: string;
	title: string;
	message: string;
	confirmText?: string;
	cancelText?: string;
	isDanger?: boolean;
	iconType?: "danger" | "warning" | "info" | "question";
	onConfirm: () => Promise<void> | void;
	onCancel?: () => void;
}

export interface AlertModalOptions {
	title: string;
	message: string;
	type?: "error" | "warning" | "info";
	buttonText?: string;
	onDismiss?: () => void;
}

/**
 * Format any error into a clean user-facing title and message.
 */
export function formatApiError(err: unknown): { title: string; message: string; status?: number } {
	if (err instanceof ApiError) {
		const title = err.title || (err.status >= 500 ? "Server Error" : "Request Error");
		return {
			title: `${title} (${err.status})`,
			message: err.message || "An unexpected error occurred on the server.",
			status: err.status,
		};
	}

	if (err instanceof Error) {
		return {
			title: "Application Error",
			message: err.message || "An unexpected error occurred.",
		};
	}

	if (typeof err === "string") {
		return {
			title: "Notice",
			message: err,
		};
	}

	return {
		title: "Unexpected Error",
		message: "An unknown error occurred. Please try again.",
	};
}

let toastCounter = 0;

/**
 * Global toast dispatcher for immediate, non-blocking visual feedback.
 */
export const toast = {
	show(options: ToastOptions): string {
		const id = `toast-${Date.now()}-${++toastCounter}`;
		const toastItem: ToastItem = {
			id,
			type: options.type || "info",
			title: options.title,
			message: options.message,
			duration: options.duration !== undefined ? options.duration : 4500,
			action: options.action,
			createdAt: Date.now(),
		};

		if (typeof window !== "undefined") {
			window.dispatchEvent(new CustomEvent("hsm:toast", { detail: toastItem }));
		}
		return id;
	},

	success(message: string, title?: string, duration = 4000): string {
		return this.show({ type: "success", message, title: title || "Success", duration });
	},

	error(errorOrMessage: unknown, title?: string, duration = 6000): string {
		if (errorOrMessage instanceof ApiError || errorOrMessage instanceof Error) {
			const formatted = formatApiError(errorOrMessage);
			return this.show({
				type: "error",
				message: formatted.message,
				title: title || formatted.title,
				duration,
			});
		}

		return this.show({
			type: "error",
			message: typeof errorOrMessage === "string" ? errorOrMessage : "Operation failed.",
			title: title || "Action Failed",
			duration,
		});
	},

	warning(message: string, title?: string, duration = 5000): string {
		return this.show({ type: "warning", message, title: title || "Warning", duration });
	},

	info(message: string, title?: string, duration = 4000): string {
		return this.show({ type: "info", message, title: title || "Notice", duration });
	},

	dismiss(id: string): void {
		if (typeof window !== "undefined") {
			window.dispatchEvent(new CustomEvent("hsm:toast:dismiss", { detail: { id } }));
		}
	},
};

/**
 * Display a modern modal confirmation dialog with async support.
 * Returns a Promise that resolves to true if confirmed, false if cancelled.
 */
export function showConfirm(options: ConfirmDialogOptions): Promise<boolean> {
	return new Promise<boolean>((resolve) => {
		if (typeof window === "undefined") {
			resolve(false);
			return;
		}

		const eventDetail: ConfirmDialogOptions = {
			...options,
			onConfirm: async () => {
				try {
					await options.onConfirm();
					resolve(true);
				} catch (err) {
					toast.error(err);
					resolve(false);
				}
			},
			onCancel: () => {
				options.onCancel?.();
				resolve(false);
			},
		};

		window.dispatchEvent(new CustomEvent("hsm:confirm", { detail: eventDetail }));
	});
}

/**
 * Display a modal alert dialog for critical notifications.
 */
export function showAlert(options: AlertModalOptions): Promise<void> {
	return new Promise<void>((resolve) => {
		if (typeof window === "undefined") {
			resolve();
			return;
		}

		const confirmOptions: ConfirmDialogOptions = {
			title: options.title,
			message: options.message,
			confirmText: options.buttonText || "Understood",
			cancelText: "",
			isDanger: options.type === "error",
			iconType: options.type === "error" ? "danger" : options.type || "info",
			onConfirm: () => {
				options.onDismiss?.();
				resolve();
			},
			onCancel: () => {
				options.onDismiss?.();
				resolve();
			},
		};

		window.dispatchEvent(new CustomEvent("hsm:confirm", { detail: confirmOptions }));
	});
}
