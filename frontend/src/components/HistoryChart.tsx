/**
 * Minimal hand-rolled SVG line chart with gradient fill for a single metric series.
 */
import { useId, useMemo } from "react";

export interface HistoryPoint {
	time: string;
	value: number;
}

export interface HistoryChartProps {
	points: HistoryPoint[];
	height?: number;
	color?: string;
}

const PAD_X = 6;
const PAD_Y = 10;

function fmt(n: number): string {
	return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

export default function HistoryChart({ points, height = 160, color = "#06b6d4" }: HistoryChartProps) {
	const gradientId = useId();

	const { linePoints, areaPoints, lastY, minValue, maxValue } = useMemo(() => {
		if (points.length < 2) {
			return { linePoints: "", areaPoints: "", lastY: height / 2, minValue: 0, maxValue: 0 };
		}

		const width = 600;
		const values = points.map((p) => p.value);
		const minVal = Math.min(...values);
		const maxVal = Math.max(...values);
		const range = maxVal - minVal || 1;
		const plotHeight = height - 2 * PAD_Y;

		const coords = points.map((point, index) => {
			const x = PAD_X + (index / (points.length - 1)) * (width - 2 * PAD_X);
			const y = PAD_Y + (1 - (point.value - minVal) / range) * plotHeight;
			return { x, y };
		});

		const lineStr = coords.map((c) => `${fmt(c.x)},${fmt(c.y)}`).join(" ");
		const areaStr = `${fmt(coords[0].x)},${height} ${lineStr} ${fmt(coords[coords.length - 1].x)},${height}`;

		return {
			linePoints: lineStr,
			areaPoints: areaStr,
			lastY: coords[coords.length - 1].y,
			minValue: minVal,
			maxValue: maxVal,
		};
	}, [points, height]);

	const viewBox = `0 0 600 ${height}`;

	return (
		<div className='history-chart-wrapper'>
			<svg className='history-chart' viewBox={viewBox} role='img' aria-label='Resource history time series chart'>
				<defs>
					<linearGradient id={gradientId} x1='0' y1='0' x2='0' y2='1'>
						<stop offset='0%' stopColor={color} stopOpacity='0.35' />
						<stop offset='100%' stopColor={color} stopOpacity='0.0' />
					</linearGradient>
				</defs>

				{/* Subtle background grid lines */}
				<line x1='0' y1={height / 4} x2='600' y2={height / 4} stroke='rgba(255,255,255,0.04)' strokeDasharray='4 4' />
				<line x1='0' y1={height / 2} x2='600' y2={height / 2} stroke='rgba(255,255,255,0.04)' strokeDasharray='4 4' />
				<line
					x1='0'
					y1={(3 * height) / 4}
					x2='600'
					y2={(3 * height) / 4}
					stroke='rgba(255,255,255,0.04)'
					strokeDasharray='4 4'
				/>

				{linePoints ?
					<>
						<polygon points={areaPoints} fill={`url(#${gradientId})`} />
						<polyline
							points={linePoints}
							fill='none'
							stroke={color}
							strokeWidth={2}
							strokeLinecap='round'
							strokeLinejoin='round'
						/>
						{/* Glowing end point */}
						<circle
							cx={600 - PAD_X}
							cy={lastY}
							r={4}
							fill={color}
							stroke='#080c14'
							strokeWidth={2}
							filter='drop-shadow(0px 0px 4px rgba(6,182,212,0.8))'
						/>
					</>
				:	<line
						x1={PAD_X}
						y1={height / 2}
						x2={600 - PAD_X}
						y2={height / 2}
						stroke='rgba(255,255,255,0.1)'
						strokeWidth={1}
					/>
				}
			</svg>
		</div>
	);
}
