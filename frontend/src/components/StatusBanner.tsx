/**
 * Service-degradation banner.
 *
 * Polls the backend's public health endpoint and, when the system is not fully
 * healthy, shows a persistent bar at the top of the page. It renders nothing at
 * all while everything is working, so it costs no layout in the normal case.
 *
 * This is the visible half of decision 7.10: when LXD is unreachable the API
 * keeps answering with the last values it recorded rather than hanging, and the
 * frontend's job is to say so out loud. Without this banner, stale numbers look
 * exactly like live ones — an operator would read a container's RAM figure from
 * twenty minutes ago as current, and only notice something was wrong when a
 * start/stop returned a 503 for no apparent reason.
 */
import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/api";
import { toast } from "../lib/alerts";

/** Response body of GET /health (backend/resources/health.py). */
interface HealthResponse {
	/** "ok" when LXD answered, "degraded" when it did not. */
	status: string;
	/** The field this banner keys on; `status` is derived from it server-side. */
	lxd_reachable: boolean;
}

/**
 * How often to re-check health, in milliseconds.
 *
 * 30 seconds, per Step 23.1. Deliberately much slower than MetricTile's
 * poll, and the reason is the cost of the call rather than the
 * freshness of the answer: /metrics/latest reads a stored sample and never
 * touches LXD (decision 7.5), but /health probes LXD on every request via
 * check_lxd_reachable(). So this interval is real load on the daemon, once per
 * 30s per open tab, and it should not be tightened without a reason.
 *
 * 30s also bounds how long a stale page can lie: an operator sees the banner
 * within half a minute of LXD going away, which is well inside the time it
 * takes to act on it.
 */
const POLL_INTERVAL_MS = 30_000;

/**
 * What the last check found.
 *
 * "unreachable" is a third state on purpose. /health always answers 200 while
 * the API process is alive, so a *failed* health request means something worse
 * than degraded — the API itself is down, restarting, or unreachable from this
 * browser. Collapsing that into "ok" would leave the banner silent in the one
 * case where nothing on the page works at all.
 */
type Health = "ok" | "degraded" | "unreachable";

export default function StatusBanner() {
	const [health, setHealth] = useState<Health>("ok");
	const previousHealth = useRef<Health>("ok");

	useEffect(() => {
		let cancelled = false;

		async function check(): Promise<void> {
			try {
				const result = await apiFetch<HealthResponse>("/health");
				if (cancelled) return;

				const nextHealth: Health = result.lxd_reachable ? "ok" : "degraded";

				if (previousHealth.current !== nextHealth) {
					if (nextHealth === "degraded") {
						toast.warning(
							"LXD daemon is not responding. System operating in degraded mode with cached metrics.",
							"Degraded Mode",
						);
					} else if (previousHealth.current !== "ok" && nextHealth === "ok") {
						toast.success("LXD daemon connection re-established. System operating normally.", "System Restored");
					}
					previousHealth.current = nextHealth;
				}

				setHealth(nextHealth);
			} catch {
				if (cancelled) return;
				const nextHealth: Health = "unreachable";
				if (previousHealth.current !== nextHealth) {
					toast.error("Cannot reach the backend API server. Telemetry paused.", "Server Unreachable");
					previousHealth.current = nextHealth;
				}
				setHealth("unreachable");
			}
		}

		// Check once immediately so a page loaded during an outage does not look
		// healthy for a full interval, then settle into the fixed cadence.
		void check();
		const timerId = setInterval(check, POLL_INTERVAL_MS);

		// Clearing the interval on unmount is what stops the banner from leaving
		// a live timer behind — and each of those timers probes LXD.
		return () => {
			cancelled = true;
			clearInterval(timerId);
		};
	}, []);

	// Nothing to say while healthy. Returning null rather than an empty element
	// keeps the banner out of the layout entirely in the common case.
	if (health === "ok") return null;

	return (
		// role="status" makes this a polite live region, so a screen reader
		// announces the banner when it appears mid-session instead of only when
		// the user happens to navigate back to the top of the page.
		<p className={`status-banner ${health}`} role='status'>
			{health === "degraded" ?
				"LXD is not responding. Figures on this page are the last values " +
				"recorded, not live, and starting, stopping or creating containers " +
				"will fail until it recovers."
			:	"Cannot reach the server. Nothing on this page is live — it may be " + "restarting."}
		</p>
	);
}
