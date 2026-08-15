/**
 * Time-Driven Real-Time Streaming SVG Chart.
 *
 * The X-axis is mapped to wall-clock timestamps, NOT array index.
 * The NEWEST point is always pinned to the RIGHT EDGE.
 * When a new 5-second sample arrives, every previous point shifts left
 * proportionally to the time gap between it and the newest point.
 *
 * Features:
 * - Time-proportional X-axis: newest point pinned right, older points shift left
 * - Smooth Monotone Cubic Bezier Spline interpolation
 * - Pulsing live radar beacon on the newest point
 * - Interactive hover crosshair with glassmorphism tooltip
 * - Real-time stats header (Current / Min / Avg / Max)
 * - Relative time-axis tick labels
 */
import { useId, useMemo, useState, useRef, useCallback, type MouseEvent } from "react";

export interface HistoryPoint {
	time: string;
	value: number;
}

export interface HistoryChartProps {
	points: HistoryPoint[];
	height?: number;
	color?: string;
	unit?: string;
	formatValue?: (v: number) => string;
	isLive?: boolean;
	timeWindowLabel?: string;
}

const PAD_X = 14;
const PAD_Y = 18;
const CHART_WIDTH = 600;

function fmt(n: number): string {
	return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

function formatRelativeTime(dateStr: string): string {
	try {
		const date = new Date(dateStr);
		return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
	} catch {
		return dateStr;
	}
}

/**
 * Builds a smooth cubic bezier SVG path (Catmull-Rom style).
 */
function buildSmoothPath(coords: Array<{ x: number; y: number }>): string {
	if (coords.length === 0) return "";
	if (coords.length === 1) return `M ${fmt(coords[0].x)} ${fmt(coords[0].y)}`;
	if (coords.length === 2) {
		return `M ${fmt(coords[0].x)} ${fmt(coords[0].y)} L ${fmt(coords[1].x)} ${fmt(coords[1].y)}`;
	}

	let d = `M ${fmt(coords[0].x)} ${fmt(coords[0].y)}`;

	for (let i = 0; i < coords.length - 1; i++) {
		const p0 = coords[i === 0 ? 0 : i - 1];
		const p1 = coords[i];
		const p2 = coords[i + 1];
		const p3 = coords[i + 2 < coords.length ? i + 2 : i + 1];

		const cp1x = p1.x + (p2.x - p0.x) / 6;
		const cp1y = p1.y + (p2.y - p0.y) / 6;
		const cp2x = p2.x - (p3.x - p1.x) / 6;
		const cp2y = p2.y - (p3.y - p1.y) / 6;

		d += ` C ${fmt(cp1x)} ${fmt(cp1y)}, ${fmt(cp2x)} ${fmt(cp2y)}, ${fmt(p2.x)} ${fmt(p2.y)}`;
	}

	return d;
}

// Live mode: 1 minute (60s) shown on screen
const LIVE_WINDOW_MS = 60_000;

export default function HistoryChart({
	points,
	height = 180,
	color = "#0284c7",
	unit = "",
	formatValue,
	isLive = true,
	timeWindowLabel = "Live (5s)",
}: HistoryChartProps) {
	const gradientId = useId();
	const glowFilterId = useId();
	const clipPathId = useId();
	const [hoverIndex, setHoverIndex] = useState<number | null>(null);
	const svgRef = useRef<SVGSVGElement | null>(null);

	const { pathData, areaPathData, coords, lastX, lastY, minValue, maxValue, avgValue, curValue } = useMemo(() => {
		if (points.length === 0) {
			return {
				pathData: "",
				areaPathData: "",
				coords: [],
				lastX: CHART_WIDTH - PAD_X,
				lastY: height / 2,
				minValue: 0,
				maxValue: 0,
				avgValue: 0,
				curValue: 0,
			};
		}

		const values = points.map((p) => p.value);
		const minVal = Math.min(...values);
		const maxVal = Math.max(...values);
		const sumVal = values.reduce((a, b) => a + b, 0);
		const avgVal = sumVal / values.length;
		const curVal = values[values.length - 1];

		const range = maxVal - minVal || Math.max(1, maxVal * 0.1);
		const plotHeight = height - 2 * PAD_Y;
		const plotWidth = CHART_WIDTH - 2 * PAD_X;

		// Parse all timestamps once
		const parsed = points.map((p) => ({ ...p, ms: Date.parse(p.time) })).filter((p) => Number.isFinite(p.ms));
		if (parsed.length < 2) {
			const singleVal = parsed.length === 1 ? parsed[0].value : curVal;
			return {
				pathData: "",
				areaPathData: "",
				coords: [],
				lastX: CHART_WIDTH - PAD_X,
				lastY: height / 2,
				minValue: minVal,
				maxValue: maxVal,
				avgValue: avgVal,
				curValue: singleVal,
			};
		}

		// Sort by time
		parsed.sort((a, b) => a.ms - b.ms);

		// NEWEST point is always pinned to the RIGHT EDGE.
		// The left edge is (newestTime - windowDuration).
		// When a new measurement arrives, the old "newest" shifts left, and
		// the new point takes the right edge. Gaps between points reflect
		// real time intervals.
		const newestMs = parsed[parsed.length - 1].ms;
		const oldestMs = parsed[0].ms;

		let windowSpanMs: number;
		if (isLive) {
			// Live: show last 2.5 minutes, or the actual data span if shorter
			windowSpanMs = Math.max(newestMs - oldestMs, LIVE_WINDOW_MS);
		} else {
			// Historical: span from first to last point
			windowSpanMs = Math.max(newestMs - oldestMs, 60_000);
		}

		const windowStartMs = newestMs - windowSpanMs;

		// Map timestamp → X. Newest point maps to plotWidth (right edge).
		const computedCoords = parsed.map((p) => {
			const ratio = (p.ms - windowStartMs) / windowSpanMs;
			const x = PAD_X + Math.max(0, Math.min(1, ratio)) * plotWidth;
			const y = PAD_Y + (1 - (p.value - minVal) / range) * plotHeight;
			return { x, y, point: { time: p.time, value: p.value }, pMs: p.ms };
		});

		const linePath = buildSmoothPath(computedCoords);
		const firstCoord = computedCoords[0];
		const lastCoord = computedCoords[computedCoords.length - 1];
		const areaPath = `${linePath} L ${fmt(lastCoord.x)} ${height} L ${fmt(firstCoord.x)} ${height} Z`;

		return {
			pathData: linePath,
			areaPathData: areaPath,
			coords: computedCoords,
			lastX: lastCoord.x,
			lastY: lastCoord.y,
			minValue: minVal,
			maxValue: maxVal,
			avgValue: avgVal,
			curValue: curVal,
		};
	}, [points, height, isLive]);

	const handleMouseMove = useCallback(
		(e: MouseEvent<SVGSVGElement>) => {
			if (coords.length < 2 || !svgRef.current) return;
			const rect = svgRef.current.getBoundingClientRect();
			const mouseX = ((e.clientX - rect.left) / rect.width) * CHART_WIDTH;

			let closestIdx = 0;
			let closestDist = Infinity;
			for (let i = 0; i < coords.length; i++) {
				const dist = Math.abs(coords[i].x - mouseX);
				if (dist < closestDist) {
					closestDist = dist;
					closestIdx = i;
				}
			}
			setHoverIndex(closestIdx);
		},
		[coords],
	);

	const handleMouseLeave = useCallback(() => {
		setHoverIndex(null);
	}, []);

	const activeHoverCoord = hoverIndex !== null && coords[hoverIndex] ? coords[hoverIndex] : null;

	const displayVal = (val: number) => {
		if (formatValue) return formatValue(val);
		return `${val.toFixed(1)}${unit ? " " + unit : ""}`;
	};

	const viewBox = `0 0 ${CHART_WIDTH} ${height}`;

	return (
		<div className='history-chart-wrapper'>
			{/* Stats header */}
			<div className='chart-stat-strip'>
				<div className='chart-stat-item current'>
					<span className='stat-label'>CURRENT</span>
					<span className='stat-value highlight' style={{ color }}>
						{displayVal(curValue)}
					</span>
				</div>
				<div className='chart-stat-group'>
					<div className='chart-stat-item'>
						<span className='stat-label'>MIN</span>
						<span className='stat-value'>{displayVal(minValue)}</span>
					</div>
					<div className='chart-stat-item'>
						<span className='stat-label'>AVG</span>
						<span className='stat-value'>{displayVal(avgValue)}</span>
					</div>
					<div className='chart-stat-item'>
						<span className='stat-label'>MAX</span>
						<span className='stat-value'>{displayVal(maxValue)}</span>
					</div>
				</div>
			</div>

			<div className='svg-chart-container'>
				<svg
					ref={svgRef}
					className='history-chart'
					viewBox={viewBox}
					role='img'
					aria-label='Resource telemetry time series graph'
					onMouseMove={handleMouseMove}
					onMouseLeave={handleMouseLeave}>
					<defs>
						<linearGradient id={gradientId} x1='0' y1='0' x2='0' y2='1'>
							<stop offset='0%' stopColor={color} stopOpacity='0.42' />
							<stop offset='70%' stopColor={color} stopOpacity='0.08' />
							<stop offset='100%' stopColor={color} stopOpacity='0.0' />
						</linearGradient>
						<filter id={glowFilterId} x='-20%' y='-20%' width='140%' height='140%'>
							<feGaussianBlur stdDeviation='2.5' result='blur' />
							<feComposite in='SourceGraphic' in2='blur' operator='over' />
						</filter>
						<clipPath id={clipPathId}>
							<rect x={PAD_X} y={0} width={CHART_WIDTH - 2 * PAD_X} height={height} />
						</clipPath>
					</defs>

					{/* Horizontal guide lines */}
					<g className={`chart-grid-lines ${isLive ? "live-moving-grid" : ""}`}>
						<line x1={0} y1={PAD_Y} x2={CHART_WIDTH} y2={PAD_Y} stroke='rgba(15, 23, 42, 0.06)' strokeDasharray='4 4' />
						<line
							x1={0}
							y1={height / 2}
							x2={CHART_WIDTH}
							y2={height / 2}
							stroke='rgba(15, 23, 42, 0.06)'
							strokeDasharray='4 4'
						/>
						<line x1={0} y1={height - PAD_Y} x2={CHART_WIDTH} y2={height - PAD_Y} stroke='rgba(15, 23, 42, 0.08)' />
					</g>

					{pathData ?
						<g clipPath={`url(#${clipPathId})`}>
							{/* Waveform Area and Line */}
							<g className='chart-wave-stream'>
								<path d={areaPathData} fill={`url(#${gradientId})`} className='chart-area-fill' />
								<path
									d={pathData}
									fill='none'
									stroke={color}
									strokeWidth={2.4}
									strokeLinecap='round'
									strokeLinejoin='round'
									filter={`url(#${glowFilterId})`}
									className='chart-curve-line'
								/>
							</g>

							{/* Data node dots along the curve */}
							<g className='chart-nodes-group'>
								{coords.map((c, i) => {
									const isNewest = i === coords.length - 1;
									return (
										<circle
											key={`node-${c.point.time}-${i}`}
											cx={c.x}
											cy={c.y}
											r={isNewest ? 4.5 : 2.5}
											fill={isNewest ? color : "#ffffff"}
											stroke={color}
											strokeWidth={isNewest ? 2 : 1.5}
											opacity={isNewest ? 1 : 0.85}
											className='chart-node-dot'
										/>
									);
								})}
							</g>

							{/* Pulsing beacon on the newest leading point */}
							{isLive && (
								<g className='live-beacon-group'>
									<circle cx={lastX} cy={lastY} r={12} fill={color} opacity={0.25} className='live-beacon-pulse' />
									<circle cx={lastX} cy={lastY} r={4.5} fill={color} stroke='#ffffff' strokeWidth={2} />
								</g>
							)}

							{/* Hover crosshair */}
							{activeHoverCoord && (
								<g className='chart-hover-group'>
									<line
										x1={activeHoverCoord.x}
										y1={PAD_Y}
										x2={activeHoverCoord.x}
										y2={height - PAD_Y}
										stroke='rgba(15, 23, 42, 0.45)'
										strokeDasharray='3 3'
										strokeWidth={1.5}
									/>
									<circle
										cx={activeHoverCoord.x}
										cy={activeHoverCoord.y}
										r={5.5}
										fill={color}
										stroke='#ffffff'
										strokeWidth={2.5}
									/>
								</g>
							)}
						</g>
					:	<g className='empty-chart-fallback'>
							<line
								x1={PAD_X}
								y1={height / 2}
								x2={CHART_WIDTH - PAD_X}
								y2={height / 2}
								stroke='rgba(15, 23, 42, 0.12)'
								strokeDasharray='6 6'
								strokeWidth={1.5}
							/>
							<text
								x={CHART_WIDTH / 2}
								y={height / 2 - 8}
								textAnchor='middle'
								fill='var(--text-muted)'
								fontSize='12'
								fontFamily='var(--font-sans)'>
								Collecting telemetry points…
							</text>
						</g>
					}
				</svg>

				{/* Hover tooltip */}
				{activeHoverCoord && (
					<div
						className='chart-hover-tooltip'
						style={{
							left: `${(activeHoverCoord.x / CHART_WIDTH) * 100}%`,
							top: `${(activeHoverCoord.y / height) * 100}%`,
						}}>
						<div className='tooltip-val' style={{ color }}>
							{displayVal(activeHoverCoord.point.value)}
						</div>
						<div className='tooltip-time'>{formatRelativeTime(activeHoverCoord.point.time)}</div>
					</div>
				)}
			</div>

			{/* Time axis labels */}
			<div className='chart-time-axis'>
				{isLive ?
					<>
						<span>-60s</span>
						<span>-45s</span>
						<span>-30s</span>
						<span>-15s</span>
						<span>-5s</span>
						<span className='axis-live-now'>
							<span className='live-dot'></span> NOW
						</span>
					</>
				:	<>
						<span>Start of window</span>
						<span>Mid-point</span>
						<span>{timeWindowLabel}</span>
					</>
				}
			</div>
		</div>
	);
}
