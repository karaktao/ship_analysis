"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

type Severity = "info" | "warning" | "critical";
type HealthStatus = "healthy" | "warning" | "critical" | "partial" | "no_data";

type Reason = {
  severity: Severity;
  code: string;
  message: string;
};

type Period = {
  periodStart: string;
  periodEnd?: string;
  received: number;
  new: number;
  existing?: number;
  observedRuns?: number;
  completedRuns: number;
  failedRuns: number;
  runningRuns?: number;
  expectedRuns?: number;
  completionRate?: number;
  requestSuccessRate?: number;
  paginationChanges?: number;
  averageSeconds?: number | null;
  maxSeconds?: number | null;
  label?: string;
};

type DashboardData = {
  schemaVersion: number;
  mode: string;
  generatedAt: string;
  timezone: string;
  health: {
    status: HealthStatus;
    headline: string;
    hasAnomaly: boolean;
    reasons: Reason[];
  };
  collector: {
    status: "collecting" | "online" | "stopped" | "no_data";
    runningRequests: number;
    lastRunAt: string | null;
    freshnessSeconds: number | null;
    observationWindowSeconds: number;
    latestTile: string | null;
    latestItems: number;
    latestError: string | null;
    lastFailedAt: string | null;
    lastFailedTile: string | null;
    lastFailedError: string | null;
    targetCount: number;
    intervalSeconds: number;
  };
  volume: {
    lastHour: Period;
    operatingDay: Period;
    hourly: Period[];
  };
  storage: {
    dataBytes: number;
    databaseBytes: number;
    logicalDatabaseBytes: number;
    walBytes: number;
    rawBytes: number;
    archiveBytes: number;
    logBytes: number;
    diskTotalBytes: number;
    diskFreeBytes: number;
    diskUsedPercent: number;
    rawRetentionHours: number;
    runDetailDays: number;
  };
  maintenance: {
    lastCleanupAt: string | null;
    compaction: {
      operational_date: string;
      status: string;
      source_sample_count: number | null;
      output_record_count: number | null;
      completed_at_utc: string | null;
      error: string | null;
    } | null;
  };
  latestDailySummary: {
    operationalDate: string;
    generatedAt: string;
    healthStatus: string;
    received: number;
    new: number;
    activeMinutes: number;
    expectedMinutes: number;
  } | null;
};

const integer = new Intl.NumberFormat("en-US");
const percent = new Intl.NumberFormat("en-US", {
  style: "percent",
  maximumFractionDigits: 1,
});

function formatBytes(value: number) {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(
    units.length - 1,
    Math.floor(Math.log(value) / Math.log(1024)),
  );
  return `${(value / 1024 ** index).toFixed(index >= 3 ? 1 : 0)} ${units[index]}`;
}

function formatTime(value: string | null, timezone: string) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone,
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function freshness(seconds: number | null) {
  if (seconds === null) return "No data";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

async function fetchDashboard(): Promise<DashboardData> {
  const local =
    typeof window !== "undefined" &&
    ["localhost", "127.0.0.1"].includes(window.location.hostname);
  const candidates = [
    "/api/dashboard",
    ...(local ? ["http://127.0.0.1:8765/api/dashboard"] : []),
    "/dashboard-snapshot.json",
  ];
  let lastError: unknown = null;
  for (const url of candidates) {
    try {
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as DashboardData;
      if (payload.schemaVersion !== 2) {
        throw new Error("Dashboard snapshot schema is out of date");
      }
      return payload;
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError instanceof Error
    ? lastError
    : new Error("Dashboard data is unavailable");
}

export function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await fetchDashboard();
      setData(next);
      setError(null);
      setLastRefresh(new Date());
    } catch (refreshError) {
      setError(
        refreshError instanceof Error
          ? refreshError.message
          : "Dashboard data is unavailable",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void refresh(), 0);
    const timer = window.setInterval(() => void refresh(), 30_000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [refresh]);

  const peak = useMemo(
    () => Math.max(1, ...(data?.volume.hourly.map((row) => row.received) ?? [1])),
    [data],
  );

  if (loading && !data) {
    return (
      <main className="state-shell">
        <div className="state-pulse" />
        <h1>Loading AIS collection health</h1>
        <p>Reading the latest aggregate status.</p>
      </main>
    );
  }

  if (!data) {
    return (
      <main className="state-shell state-error">
        <span>!</span>
        <h1>Dashboard connection unavailable</h1>
        <p>{error ?? "Start the local dashboard API and try again."}</p>
        <button type="button" onClick={() => void refresh()}>
          Retry
        </button>
      </main>
    );
  }

  const healthLabel = {
    healthy: "Healthy",
    warning: "Warning",
    critical: "Critical",
    partial: "Starting",
    no_data: "No data",
  }[data.health.status];
  const compaction = data.maintenance.compaction;
  const scheduleBaselineReady =
    data.collector.observationWindowSeconds >= 3600;

  return (
    <main className="dashboard">
      <header className="topbar">
        <div>
          <p className="eyebrow">NETHERLANDS AIS COLLECTION</p>
          <h1>AIS Collection Health</h1>
        </div>
        <div className="topbar-actions">
          <span className={`health-pill health-${data.health.status}`}>
            <i />
            {healthLabel}
          </span>
          <button type="button" onClick={() => void refresh()}>
            Refresh
          </button>
        </div>
      </header>

      {error && (
        <div className="notice">
          Live API unavailable; showing the last successful snapshot. {error}
        </div>
      )}

      <section className={`health-summary health-${data.health.status}`}>
        <div>
          <p>System status</p>
          <h2>{data.health.headline}</h2>
          <span>
            Last collection {freshness(data.collector.freshnessSeconds)} ·{" "}
            {data.collector.latestTile ?? "no grid"}
          </span>
        </div>
        <dl>
          <div>
            <dt>Collection cadence</dt>
            <dd>{data.collector.intervalSeconds}s</dd>
          </div>
          <div>
            <dt>National grids</dt>
            <dd>{data.collector.targetCount}</dd>
          </div>
          <div>
            <dt>Last refresh</dt>
            <dd>
              {lastRefresh
                ? lastRefresh.toLocaleTimeString("en-GB", { hour12: false })
                : "—"}
            </dd>
          </div>
        </dl>
      </section>

      <section className="metric-grid" aria-label="Key collection metrics">
        <MetricCard
          label="Operating-day API items"
          value={integer.format(data.volume.operatingDay.received)}
          detail={`${integer.format(data.volume.operatingDay.new)} new observations`}
        />
        <MetricCard
          label="Last-hour new observations"
          value={integer.format(data.volume.lastHour.new)}
          detail={`${integer.format(data.volume.lastHour.received)} API items received`}
        />
        <MetricCard
          label="Last-hour completion"
          value={
            scheduleBaselineReady
              ? percent.format(data.volume.lastHour.completionRate ?? 0)
              : "Warming up"
          }
          detail={
            scheduleBaselineReady
              ? `${integer.format(data.volume.lastHour.failedRuns)} failed requests`
              : `${integer.format(data.volume.lastHour.completedRuns)} requests completed`
          }
          tone={
            !scheduleBaselineReady
              ? undefined
              : (data.volume.lastHour.completionRate ?? 0) >= 0.98
              ? "good"
              : "warning"
          }
        />
        <MetricCard
          label="Disk space remaining"
          value={formatBytes(data.storage.diskFreeBytes)}
          detail={`${percent.format(data.storage.diskUsedPercent)} of disk used`}
          tone={data.storage.diskUsedPercent < 0.8 ? "good" : "warning"}
        />
        <MetricCard
          label="Collection data on disk"
          value={formatBytes(data.storage.dataBytes)}
          detail={`${formatBytes(data.storage.rawBytes)} raw · ${data.storage.rawRetentionHours}h retention`}
        />
        <MetricCard
          label="SQLite WAL"
          value={formatBytes(data.storage.walBytes)}
          detail={`${formatBytes(data.storage.logicalDatabaseBytes)} logical database`}
          tone={
            data.storage.walBytes >
            Math.max(1024 ** 3, data.storage.logicalDatabaseBytes * 2)
              ? "warning"
              : "good"
          }
        />
      </section>

      <section className="content-grid">
        <article className="panel trend-panel">
          <PanelHeader
            eyebrow="VOLUME"
            title="API items received · last 24 hours"
            meta={`Peak ${integer.format(peak)}`}
          />
          <div className="bar-chart" aria-label="Hourly collection volume">
            {data.volume.hourly.map((row, index) => (
              <div className="bar-slot" key={row.periodStart}>
                <div
                  className={`bar ${row.failedRuns ? "bar-alert" : ""}`}
                  style={{
                    height: `${Math.max(2, (row.received / peak) * 100)}%`,
                  }}
                  title={`${row.label}: ${integer.format(row.received)} items; ${integer.format(row.new)} new`}
                />
                <small>
                  {index % 4 === 0 || index === data.volume.hourly.length - 1
                    ? row.label
                    : ""}
                </small>
              </div>
            ))}
          </div>
          <div className="legend">
            <span><i className="legend-main" />API items</span>
            <span><i className="legend-alert" />Hour with failures</span>
          </div>
        </article>

        <article className="panel anomaly-panel">
          <PanelHeader
            eyebrow="ANOMALY CHECK"
            title={data.health.hasAnomaly ? "Items to investigate" : "No active anomaly"}
            meta={healthLabel}
          />
          {data.health.reasons.length ? (
            <ul className="reason-list">
              {data.health.reasons.map((reason) => (
                <li className={`reason-${reason.severity}`} key={reason.code}>
                  <i />
                  <div>
                    <strong>{reason.code.replaceAll("_", " ")}</strong>
                    <span>{reason.message}</span>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <div className="all-clear">
              <span>✓</span>
              <div>
                <strong>All monitored checks passed</strong>
                <p>Collection freshness, requests, disk and maintenance are within limits.</p>
              </div>
            </div>
          )}
        </article>
      </section>

      <section className="panel maintenance-panel">
        <PanelHeader
          eyebrow="STORAGE & MAINTENANCE"
          title="Daily processing"
          meta={`Generated ${formatTime(data.generatedAt, data.timezone)}`}
        />
        <div className="maintenance-grid">
          <MaintenanceItem
            label="Latest compaction"
            value={compaction?.status ?? "Pending"}
            detail={
              compaction
                ? `${compaction.operational_date} · ${integer.format(compaction.output_record_count ?? 0)} records`
                : "No completed operating day yet"
            }
          />
          <MaintenanceItem
            label="Last staging cleanup"
            value={formatTime(data.maintenance.lastCleanupAt, data.timezone)}
            detail={`${data.storage.rawRetentionHours}h raw retention · ${data.storage.runDetailDays}d request details`}
          />
          <MaintenanceItem
            label="Compressed archive"
            value={formatBytes(data.storage.archiveBytes)}
            detail="Daily gzip archive retained"
          />
          <MaintenanceItem
            label="Database files"
            value={formatBytes(
              data.storage.databaseBytes + data.storage.walBytes,
            )}
            detail={`${formatBytes(data.storage.logBytes)} rotating logs`}
          />
        </div>
      </section>

      <footer>
        <span>Aggregate operational metrics only</span>
        <span>EuRIS Tracks · Europe/Amsterdam · 04:00 operating day</span>
      </footer>
    </main>
  );
}

function MetricCard({
  label,
  value,
  detail,
  tone = "neutral",
}: {
  label: string;
  value: string;
  detail: string;
  tone?: "neutral" | "good" | "warning";
}) {
  return (
    <article className={`metric-card metric-${tone}`}>
      <p>{label}</p>
      <strong>{value}</strong>
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

function MaintenanceItem({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail: string;
}) {
  return (
    <div className="maintenance-item">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}
