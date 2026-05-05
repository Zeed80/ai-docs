"use client";

interface CanvasChartProps {
  chartType?: "bar" | "line" | "pie" | "area";
  chartData?: Record<string, unknown>;
  title?: string;
}

interface ChartPoint {
  label: string;
  value: number;
}

function toNumber(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value.replace(",", "."));
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
}

function normalizePoints(chartData?: Record<string, unknown>): ChartPoint[] {
  if (!chartData) return [];

  const points = chartData.points;
  if (Array.isArray(points)) {
    return points
      .map((item, index) => {
        const row = item && typeof item === "object" ? item as Record<string, unknown> : {};
        return {
          label: String(row.label ?? row.name ?? row.x ?? index + 1),
          value: toNumber(row.value ?? row.y),
        };
      })
      .filter((point) => point.label && Number.isFinite(point.value));
  }

  const labels = Array.isArray(chartData.labels) ? chartData.labels : [];
  const values = Array.isArray(chartData.values) ? chartData.values : [];
  if (labels.length || values.length) {
    return values
      .map((value, index) => ({
        label: String(labels[index] ?? index + 1),
        value: toNumber(value),
      }))
      .filter((point) => point.label && Number.isFinite(point.value));
  }

  const rows = Array.isArray(chartData.rows) ? chartData.rows : [];
  return rows
    .map((item, index) => {
      const row = item && typeof item === "object" ? item as Record<string, unknown> : {};
      return {
        label: String(row.label ?? row.name ?? row.category ?? index + 1),
        value: toNumber(row.value ?? row.amount ?? row.count ?? row.total),
      };
    })
    .filter((point) => point.label && Number.isFinite(point.value));
}

function pathFor(points: ChartPoint[], width: number, height: number) {
  const max = Math.max(...points.map((p) => p.value), 1);
  const step = points.length <= 1 ? width : width / (points.length - 1);
  return points
    .map((point, index) => {
      const x = index * step;
      const y = height - (point.value / max) * height;
      return `${index === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
}

function pieSegments(points: ChartPoint[]) {
  const total = points.reduce((sum, point) => sum + Math.max(0, point.value), 0) || 1;
  let offset = 0;
  return points.map((point) => {
    const value = Math.max(0, point.value);
    const start = offset;
    const end = offset + value / total;
    offset = end;
    return { ...point, start, end };
  });
}

function polar(cx: number, cy: number, r: number, fraction: number) {
  const angle = fraction * Math.PI * 2 - Math.PI / 2;
  return [cx + Math.cos(angle) * r, cy + Math.sin(angle) * r];
}

export function CanvasChart({ chartType = "bar", chartData, title }: CanvasChartProps) {
  const points = normalizePoints(chartData).slice(0, 24);
  if (!points.length) {
    return <div className="text-xs text-slate-500">Нет данных для графика.</div>;
  }

  const max = Math.max(...points.map((p) => p.value), 1);
  const width = 520;
  const height = 220;
  const palette = ["#38bdf8", "#34d399", "#f59e0b", "#f472b6", "#a78bfa", "#fb7185"];

  if (chartType === "pie") {
    const segments = pieSegments(points);
    return (
      <div className="space-y-3">
        <svg viewBox="0 0 260 220" className="h-56 w-full">
          {segments.map((segment, index) => {
            const [x1, y1] = polar(130, 105, 86, segment.start);
            const [x2, y2] = polar(130, 105, 86, segment.end);
            const large = segment.end - segment.start > 0.5 ? 1 : 0;
            return (
              <path
                key={segment.label}
                d={`M 130 105 L ${x1} ${y1} A 86 86 0 ${large} 1 ${x2} ${y2} Z`}
                fill={palette[index % palette.length]}
                opacity="0.88"
              />
            );
          })}
        </svg>
        <Legend points={points} palette={palette} />
      </div>
    );
  }

  const linePath = pathFor(points, width, height);
  const areaPath = `${linePath} L ${width} ${height} L 0 ${height} Z`;
  return (
    <div className="space-y-2">
      <svg viewBox={`0 0 ${width} ${height + 34}`} className="h-64 w-full">
        <line x1="0" y1={height} x2={width} y2={height} stroke="#334155" />
        {chartType === "bar" ? (
          points.map((point, index) => {
            const gap = 6;
            const barWidth = Math.max(10, width / points.length - gap);
            const barHeight = (point.value / max) * height;
            const x = index * (width / points.length) + gap / 2;
            const y = height - barHeight;
            return (
              <g key={point.label}>
                <rect
                  x={x}
                  y={y}
                  width={barWidth}
                  height={barHeight}
                  rx="3"
                  fill={palette[index % palette.length]}
                  opacity="0.9"
                />
                <text x={x + barWidth / 2} y={height + 16} textAnchor="middle" fill="#94a3b8" fontSize="10">
                  {point.label.slice(0, 10)}
                </text>
              </g>
            );
          })
        ) : (
          <>
            {chartType === "area" && <path d={areaPath} fill="#38bdf8" opacity="0.16" />}
            <path d={linePath} fill="none" stroke="#38bdf8" strokeWidth="3" />
            {points.map((point, index) => {
              const step = points.length <= 1 ? width : width / (points.length - 1);
              const x = index * step;
              const y = height - (point.value / max) * height;
              return (
                <g key={point.label}>
                  <circle cx={x} cy={y} r="4" fill="#34d399" />
                  <text x={x} y={height + 16} textAnchor="middle" fill="#94a3b8" fontSize="10">
                    {point.label.slice(0, 10)}
                  </text>
                </g>
              );
            })}
          </>
        )}
      </svg>
      {title && <div className="text-xs text-slate-500">{title}</div>}
    </div>
  );
}

function Legend({ points, palette }: { points: ChartPoint[]; palette: string[] }) {
  return (
    <div className="grid grid-cols-2 gap-1 text-xs text-slate-300">
      {points.map((point, index) => (
        <div key={point.label} className="flex items-center gap-2">
          <span
            className="h-2.5 w-2.5 rounded-sm"
            style={{ backgroundColor: palette[index % palette.length] }}
          />
          <span className="truncate">{point.label}</span>
          <span className="ml-auto text-slate-500">{point.value}</span>
        </div>
      ))}
    </div>
  );
}
