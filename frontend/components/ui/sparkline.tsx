"use client";

interface SparklineProps {
  data: number[];
  width?: number;
  height?: number;
  color?: string;
  className?: string;
}

export function Sparkline({
  data,
  width = 80,
  height = 24,
  color = "#60a5fa",
  className = "",
}: SparklineProps) {
  if (!data || data.length < 2) {
    return (
      <span className={`inline-block text-slate-600 text-[10px] ${className}`}>
        —
      </span>
    );
  }

  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;

  const pad = 2;
  const w = width - pad * 2;
  const h = height - pad * 2;

  const points = data.map((v, i) => {
    const x = pad + (i / (data.length - 1)) * w;
    const y = pad + h - ((v - min) / range) * h;
    return `${x},${y}`;
  });

  const polyline = points.join(" ");

  const last = data[data.length - 1]!;
  const prev = data[data.length - 2]!;
  const trend = last > prev ? "↑" : last < prev ? "↓" : "→";
  const trendColor =
    last > prev ? "#34d399" : last < prev ? "#f87171" : "#94a3b8";

  return (
    <span className={`inline-flex items-center gap-1 ${className}`}>
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        className="shrink-0"
      >
        <polyline
          points={polyline}
          fill="none"
          stroke={color}
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        {/* Last point dot */}
        <circle
          cx={points[points.length - 1]!.split(",")[0]}
          cy={points[points.length - 1]!.split(",")[1]}
          r="2"
          fill={color}
        />
      </svg>
      <span style={{ color: trendColor }} className="text-[10px] font-medium">
        {trend}
      </span>
    </span>
  );
}
