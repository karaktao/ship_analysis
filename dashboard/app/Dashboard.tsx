"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

type RangeKey = "minute" | "hour" | "day";
type Health = "healthy" | "warning" | "critical" | "no_data";

type Period = {
  label: string;
  received: number;
  unique: number;
  new: number;
  existing: number;
  completedRuns: number;
  failedRuns: number;
  expectedRuns: number;
  expectedRunsSoFar: number;
  completionRate: number;
  requestSuccessRate: number;
  tilesSeen: number;
  expectedTiles: number;
  p95Seconds: number | null;
  paginationAnomalies: number;
  outsideBBox: number;
  distinctObservations: number | null;
  distinctTracks: number | null;
};

type TimelinePoint = {
  periodStart: string;
  label: string;
  received: number;
  new: number;
  completedRuns: number;
  failedRuns: number;
};

type DashboardData = {
  mode: string;
  generatedAt: string;
  timezone: string;
  collector: {
    status: "collecting" | "online" | "stopped" | "no_data";
    runningRequests: number;
    lastRunAt: string | null;
    freshnessSeconds: number | null;
    latestTile: string | null;
    latestItems: number;
    latestError: string | null;
    targetCount: number;
    intervalSeconds: number;
  };
  current: Record<RangeKey, Period>;
  timelines: Record<RangeKey, TimelinePoint[]>;
  tiles: Array<{
    tileId: string;
    row: number;
    column: number;
    status: "fresh" | "stale" | "failed" | "missing";
    received: number;
    completedRuns: number;
    failedRuns: number;
    lastRunAt: string | null;
    freshnessSeconds: number | null;
  }>;
  recentRuns: Array<{
    startedAt: string;
    tileId: string;
    status: string;
    items: number;
    new: number;
    existing: number;
    pages: number;
    elapsedSeconds: number | null;
    error: string | null;
  }>;
  latestDailySummary: {
    operationalDate: string;
    generatedAt: string;
    healthStatus: Health;
    summaryText: string;
    findings: string[];
  } | null;
  latestCompaction: {
    operational_date: string;
    status: string;
    source_sample_count: number | null;
    output_record_count: number | null;
    position_record_count: number | null;
    stationary_record_count: number | null;
    stationary_source_sample_count: number | null;
  } | null;
};

const ranges: Array<{ key: RangeKey; label: string; chartLabel: string }> = [
  { key: "minute", label: "Minute", chartLabel: "Last 60 minutes" },
  { key: "hour", label: "Hour", chartLabel: "Last 24 hours" },
  { key: "day", label: "Ops day", chartLabel: "Last 14 operating days" },
];

const number = new Intl.NumberFormat("zh-CN");
const percent = new Intl.NumberFormat("zh-CN", {
  style: "percent",
  maximumFractionDigits: 1,
});

function formatTime(value: string | null, timezone: string) {
  if (!value) return "Not available";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: timezone,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function freshness(seconds: number | null) {
  if (seconds === null) return "No collection yet";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

async function fetchDashboard(): Promise<DashboardData> {
  const local =
    typeof window !== "undefined" &&
    ["localhost", "127.0.0.1"].includes(window.location.hostname);
  const candidates = local
    ? [
        "http://127.0.0.1:8765/api/dashboard",
        "/dashboard-snapshot.json",
      ]
    : ["/dashboard-snapshot.json"];
  let lastError: unknown;
  for (const url of candidates) {
    try {
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as DashboardData;
      if (!payload.current || !payload.collector) {
        throw new Error("Invalid dashboard payload");
      }
      return payload;
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError instanceof Error ? lastError : new Error("Unable to read dashboard data");
}

export function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [range, setRange] = useState<RangeKey>("minute");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await fetchDashboard();
      setData(next);
      setLastRefresh(new Date());
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to read dashboard data");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 30_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const timeline = data?.timelines[range] ?? [];
  const current = data?.current[range];
  const maxReceived = useMemo(
    () => Math.max(1, ...timeline.map((point) => point.received)),
    [timeline],
  );
  const maxTile = useMemo(
    () => Math.max(1, ...(data?.tiles.map((tile) => tile.received) ?? [1])),
    [data],
  );

  if (loading && !data) {
    return (
      <main className="loading-shell" aria-label="Loading AIS statistics">
        <div className="loading-mark" />
        <p>Connecting to the Netherlands AIS pulse…</p>
      </main>
    );
  }

  if (!data || !current) {
    return (
      <main className="error-shell">
        <p className="eyebrow">AIS MONITOR</p>
        <h1>Dashboard connection unavailable</h1>
        <p>{error ?? "Start the local dashboard API and try again."}</p>
        <button type="button" onClick={() => void refresh()}>
          Reconnect
        </button>
      </main>
    );
  }

  const statusLabel = {
    collecting: "Collecting",
    online: "Online",
    stopped: "Collection stopped",
    no_data: "No data",
  }[data.collector.status];
  const isLive = data.mode === "live-local";
  const rangeMeta = ranges.find((item) => item.key === range) ?? ranges[0];

  return (
    <main className="dashboard">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
          <div>
            <p className="eyebrow">NETHERLANDS · AIS NETWORK</p>
            <h1>Netherlands AIS Pulse</h1>
          </div>
        </div>
        <div className="topbar-actions">
          <div className={`live-pill status-${data.collector.status}`}>
            <i aria-hidden="true" />
            {statusLabel}
          </div>
          <button
            className="refresh-button"
            type="button"
            onClick={() => void refresh()}
            aria-label="Refresh dashboard data"
          >
            ↻ Refresh
          </button>
        </div>
      </header>

      <section className="hero">
        <div>
          <p className="hero-kicker">
            {isLive ? "Local live data" : "Secure aggregate snapshot"} · 48 national grids · 60s cadence
          </p>
          <h2>
            From every request,
            <br />
            see the national waterways<span>come alive.</span>
          </h2>
        </div>
        <div className="hero-meta">
          <div>
            <small>Latest collection</small>
            <strong>{freshness(data.collector.freshnessSeconds)}</strong>
            <span>
              {data.collector.latestTile ?? "—"} ·{" "}
              {number.format(data.collector.latestItems)} items
            </span>
          </div>
          <div>
            <small>Dashboard refresh</small>
            <strong>
              {lastRefresh
                ? lastRefresh.toLocaleTimeString("zh-CN", { hour12: false })
                : "—"}
            </strong>
            <span>Auto-refreshes every 30 seconds</span>
          </div>
        </div>
      </section>

      {error && (
        <div className="notice" role="status">
          Live API unavailable; showing the last successful payload: {error}
        </div>
      )}
      {!isLive && (
        <div className="notice snapshot-notice">
          This is a build-time snapshot. Open the local dashboard for live SQLite statistics.
        </div>
      )}

      <nav className="range-switch" aria-label="Metric time range">
        {ranges.map((item) => (
          <button
            key={item.key}
            type="button"
            className={range === item.key ? "active" : ""}
            onClick={() => setRange(item.key)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <section className="metric-grid" aria-label="Key collection metrics">
        <MetricCard
          label={`${rangeMeta.label} API items`}
          value={current.received}
          suffix="items"
          detail={`${number.format(current.completedRuns)} successful requests`}
          tone="cyan"
        />
        <MetricCard
          label="New observations"
          value={current.new}
          suffix="items"
          detail={`${number.format(current.existing)} repeated observations`}
          tone="lime"
        />
        <MetricCard
          label="Schedule completion"
          value={percent.format(current.completionRate)}
          detail={`${number.format(current.completedRuns)} / ${number.format(
            current.expectedRunsSoFar,
          )} requests`}
          tone={current.completionRate >= 0.98 ? "lime" : "amber"}
        />
        <MetricCard
          label="Request success"
          value={percent.format(current.requestSuccessRate)}
          detail={`${number.format(current.failedRuns)} failed requests`}
          tone={current.failedRuns === 0 ? "cyan" : "amber"}
        />
        <MetricCard
          label="Grid coverage"
          value={`${current.tilesSeen}/${current.expectedTiles}`}
          detail={`${data.collector.intervalSeconds}s collection cadence`}
          tone="neutral"
        />
        <MetricCard
          label="P95 request time"
          value={
            current.p95Seconds === null
              ? "—"
              : `${current.p95Seconds.toFixed(2)}`
          }
          suffix={current.p95Seconds === null ? "" : "s"}
          detail={`${number.format(current.paginationAnomalies)} pagination anomalies`}
          tone="neutral"
        />
      </section>

      <section className="content-grid">
        <article className="panel throughput-panel">
          <PanelHeader
            eyebrow="THROUGHPUT"
            title={rangeMeta.chartLabel}
            meta={`Peak ${number.format(maxReceived)} items`}
          />
          <div className="bar-chart" role="img" aria-label="Collection throughput trend">
            {timeline.map((point, index) => {
              const height = Math.max(2, (point.received / maxReceived) * 100);
              const showLabel =
                timeline.length <= 14 ||
                index === 0 ||
                index === timeline.length - 1 ||
                index % Math.ceil(timeline.length / 6) === 0;
              return (
                <div className="bar-slot" key={point.periodStart}>
                  <div
                    className={`bar ${point.failedRuns ? "bar-alert" : ""}`}
                    style={{ height: `${height}%` }}
                    title={`${point.label}: ${number.format(
                      point.received,
                    )} items, ${number.format(point.new)} new`}
                  >
                    <span />
                  </div>
                  <small>{showLabel ? point.label : ""}</small>
                </div>
              );
            })}
          </div>
          <div className="chart-legend">
            <span><i className="legend-cyan" />API items returned</span>
            <span><i className="legend-amber" />Period with failures</span>
          </div>
        </article>

        <article className="panel pulse-panel">
          <PanelHeader
            eyebrow="LIVE PULSE"
            title="Live collection status"
            meta={formatTime(data.generatedAt, data.timezone)}
          />
          <div className="pulse-orbit">
            <div className={`pulse-core status-${data.collector.status}`}>
              <span>{data.collector.runningRequests || 48}</span>
              <small>national grids</small>
            </div>
          </div>
          <dl className="pulse-stats">
            <div>
              <dt>This minute</dt>
              <dd>{number.format(data.current.minute.received)}</dd>
            </div>
            <div>
              <dt>This hour</dt>
              <dd>{number.format(data.current.hour.received)}</dd>
            </div>
            <div>
              <dt>This operating day</dt>
              <dd>{number.format(data.current.day.received)}</dd>
            </div>
          </dl>
        </article>
      </section>

      <section className="content-grid lower-grid">
        <article className="panel tile-panel">
          <PanelHeader
            eyebrow="NATIONAL COVERAGE"
            title="8 × 6 national grid heat"
            meta={`${data.tiles.filter((tile) => tile.status === "fresh").length} fresh grids`}
          />
          <div className="tile-layout">
            <div className="tile-grid" aria-label="48 Netherlands collection grids">
              {data.tiles.map((tile) => {
                const intensity = Math.max(0.08, tile.received / maxTile);
                return (
                  <div
                    className={`tile tile-${tile.status}`}
                    key={tile.tileId}
                    style={{ "--heat": intensity } as React.CSSProperties}
                    title={`${tile.tileId}: ${number.format(
                      tile.received,
                    )} items, ${tile.completedRuns} successful requests`}
                  >
                    <span>{tile.tileId.replace("r", "").replace("c", "·")}</span>
                    <strong>{number.format(tile.received)}</strong>
                  </div>
                );
              })}
            </div>
            <div className="tile-legend">
              <span><i className="tile-fresh-dot" />Updated within 120s</span>
              <span><i className="tile-stale-dot" />Historical data</span>
              <span><i className="tile-failed-dot" />Latest request failed</span>
              <span><i className="tile-missing-dot" />No data</span>
              <p>Brightness represents API items returned during the operating day.</p>
            </div>
          </div>
        </article>

        <article className="panel summary-panel">
          <PanelHeader
            eyebrow="DAILY ASSESSMENT"
            title="Daily collection summary"
            meta="04:00 close"
          />
          {data.latestDailySummary ? (
            <>
              <div
                className={`health-badge health-${data.latestDailySummary.healthStatus}`}
              >
                <span>{data.latestDailySummary.operationalDate}</span>
                <strong>
                  {
                    {
                      healthy: "Healthy",
                      warning: "Needs attention",
                      critical: "Critical",
                      no_data: "No data",
                    }[data.latestDailySummary.healthStatus]
                  }
                </strong>
              </div>
              <ul className="finding-list">
                {data.latestDailySummary.findings.slice(0, 5).map((finding) => (
                  <li key={finding}>{finding}</li>
                ))}
              </ul>
            </>
          ) : (
            <div className="empty-summary">
              <span>04:15</span>
              <h3>First operating-day report is pending</h3>
              <p>
                After the operating day closes, the system evaluates schedule completion, failed requests, pagination anomalies and stationary compaction.
              </p>
            </div>
          )}
          {data.latestCompaction && (
            <div className="compaction-strip">
              <div>
                <small>Position records</small>
                <strong>
                  {number.format(data.latestCompaction.position_record_count ?? 0)}
                </strong>
              </div>
              <div>
                <small>Stationary segments</small>
                <strong>
                  {number.format(
                    data.latestCompaction.stationary_record_count ?? 0,
                  )}
                </strong>
              </div>
              <div>
                <small>Compacted samples</small>
                <strong>
                  {number.format(
                    data.latestCompaction.stationary_source_sample_count ?? 0,
                  )}
                </strong>
              </div>
            </div>
          )}
        </article>
      </section>

      <section className="panel runs-panel">
        <PanelHeader
          eyebrow="REQUEST AUDIT"
          title="Recent collection runs"
          meta={`Data time ${formatTime(data.generatedAt, data.timezone)}`}
        />
        <div className="run-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Local time</th>
                <th>Grid</th>
                <th>Status</th>
                <th>Items</th>
                <th>New</th>
                <th>Existing</th>
                <th>Pages</th>
                <th>Elapsed</th>
              </tr>
            </thead>
            <tbody>
              {data.recentRuns.map((run) => (
                <tr key={`${run.startedAt}-${run.tileId}`}>
                  <td>{formatTime(run.startedAt, data.timezone)}</td>
                  <td><code>{run.tileId}</code></td>
                  <td>
                    <span className={`run-status run-${run.status}`}>
                      {run.status === "completed"
                        ? "Success"
                        : run.status === "failed"
                          ? "Failed"
                          : "Running"}
                    </span>
                  </td>
                  <td>{number.format(run.items)}</td>
                  <td>{number.format(run.new)}</td>
                  <td>{number.format(run.existing)}</td>
                  <td>{run.pages || "—"}</td>
                  <td>
                    {run.elapsedSeconds === null
                      ? "—"
                      : `${run.elapsedSeconds.toFixed(2)} s`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <footer>
        <p>
          EuRIS Tracks · Europe/Amsterdam · Operating day 04:00–04:00
        </p>
        <p>Aggregate statistics only — no token or vessel identity details.</p>
      </footer>
    </main>
  );
}

function MetricCard({
  label,
  value,
  suffix,
  detail,
  tone,
}: {
  label: string;
  value: string | number;
  suffix?: string;
  detail: string;
  tone: "cyan" | "lime" | "amber" | "neutral";
}) {
  return (
    <article className={`metric-card metric-${tone}`}>
      <p>{label}</p>
      <strong>
        {typeof value === "number" ? number.format(value) : value}
        {suffix && <small>{suffix}</small>}
      </strong>
      <span>{detail}</span>
    </article>
  );
}

function PanelHeader({
  eyebrow,
  title,
  meta,
}: {
  eyebrow: string;
  title: string;
  meta: string;
}) {
  return (
    <header className="panel-header">
      <div>
        <p>{eyebrow}</p>
        <h3>{title}</h3>
      </div>
      <span>{meta}</span>
    </header>
  );
}
