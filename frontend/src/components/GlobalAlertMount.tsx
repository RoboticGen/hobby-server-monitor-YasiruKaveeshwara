/**
 * Unified Global Alert & Confirmation Mount Component.
 *
 * Bundles both ToastContainer and ConfirmModal into a single lightweight React island.
 * Mounted once in SiteHeader.astro to provide system-wide notification services.
 */
import ToastContainer from "./ToastContainer";
import ConfirmModal from "./ConfirmModal";

export default function GlobalAlertMount() {
	return (
		<>
			<ToastContainer />
			<ConfirmModal />
		</>
	);
}
