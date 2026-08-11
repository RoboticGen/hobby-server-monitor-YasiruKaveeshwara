/**
 * Minimal hand-rolled SVG line chart for a single metric series.
 *
 * WHY HAND-ROLLED AND NOT A CHARTING LIBRARY: this project deliberately
 * keeps dependencies near zero (rule 0.10), and a full charting library is
 * over-engineering for what is at most three single-series line charts on
 * one page. Every chart here needs exactly the same thing — map a list of
 * {time, value} pairs to a <polyline>, with axes as an afterthought — which
 * is ~80 lines of SVG. A library would add tens of kilobytes of bundle for
 * features nothing in the product uses, and it would hide the mapping
 * behind an abstraction the codebase otherwise avoids.
 */
import { useMemo } from "react";

/** One point on a series, already downsampled server-side (windowed). */
export interface HistoryPoint {
	/** ISO-8601 UTC timestamp. */
	time: string;
	/** The metric's value at that time, in whatever unit the series uses. */
	value: number;
}

export interface HistoryChartProps {
	/** Points to plot, ordered by time ascending. */
	points: HistoryPoint[];
	/** Height of the chart's plot area in SVG units (default 160). */
	height?: number;
}

/** Horizontal padding inside the SVG so a flat line at the edges is not
    clipped by the stroke. */
const PAD_X = 4;
/** Vertical padding above/below the plotted range for the same reason. */
const PAD_Y = 6;

/** Format a pixel co-ordinate as a string without trailing .0 noise. */
function fmt(n: number): string {
	return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

export default function HistoryChart({ points, height = 160 }: HistoryChartProps) {
	/**
	 * Build the SVG polyline string from the raw points.
	 *
	 * `useMemo` so this only recomputes when the data actually changes —
	 * the component re-renders every 10s when the live tile next to it
	 * refreshes, and re-mapping a few hundred points per render is
	 * unnecessary work even if it is cheap.
	 *
	 * No chart when there are fewer than two points: a single point has no
	 * line to draw, and an empty series would produce a divide-by-zero in
	 * the range calculation below.
	 */
	const linePoints = useMemo(() => {
		if (points.length < 2) return "";

		const width = 600; // fixed plot width; the SVG scales to its container
		const values = points.map((point) => point.value);
		const minValue = Math.min(...values);
		const maxValue = Math.max(...values);
		// A flat series (min === max) would divide by zero, so give it a
		// nominal range; the line then sits centred rather than vanishing.
		const range = maxValue - minValue || 1;
		const plotHeight = height - 2 * PAD_Y;

		return points
			.map((point, index) => {
				const x = PAD_X + (index / (points.length - 1)) * (width - 2 * PAD_X);
				const y = PAD_Y + (1 - (point.value - minValue) / range) * plotHeight;
				return `${fmt(x)},${fmt(y)}`;
			})
			.join(" ");
	}, [points, height]);

	const viewBox = `0 0 600 ${height}`;

	return (
		<svg
			className='history-chart'
			viewBox={viewBox}
			role='img'
			aria-label='Line chart of this metric over the selected window'>
			{
				linePoints ?
					/* stroke="currentColor" rather than a fixed colour: the line is
           visible by default (a polyline with no stroke renders as
           nothing), while the page can still restyle it by setting
           `color` on the chart, keeping the page-owns-styling rule that
           MetricTile follows. */
					<polyline points={linePoints} fill='none' stroke='currentColor' strokeWidth={1.5} strokeLinejoin='round' />
					/* A deliberately flat, silent placeholder rather than nothing:
           an empty <svg> collapses to zero height and the tile above it
           jumps, which reads as a broken render. */
				:	<line x1={PAD_X} y1={height / 2} x2={600 - PAD_X} y2={height / 2} stroke='#ccc' strokeWidth={1} />
			}
		</svg>
	);
}
