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

type CalendarDay = {
  operationalDate: string;
  generatedAt: string | null;
  healthStatus: HealthStatus;
  received: number;
  newObservations: number;
  uniqueItems: number;
  expectedRuns: number;
  observedRuns: number;
  completedRuns: number;
  failedRuns: number;
  runningRuns: number;
  completionRate: number | null;
  isFinal: boolean;
};

type CalendarPayload = {
  month: string;
  timezone: string;
  days: CalendarDay[];
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

function currentMonthKey() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

function shiftMonth(month: string, offset: number) {
  const [year, monthNumber] = month.split("-").map(Number);
  const shifted = new Date(year, monthNumber - 1 + offset, 1);
  return `${shifted.getFullYear()}-${String(shifted.getMonth() + 1).padStart(2, "0")}`;
}

function calendarStatusLabel(status: HealthStatus) {
  return {
    healthy: "Healthy",
    warning: "Warning",
    critical: "Critical",
    partial: "Partial",
    no_data: "No data",
  }[status];
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

async function fetchCalendar(month: string): Promise<CalendarPayload> {
  const response = await fetch(`/api/calendar?month=${encodeURIComponent(month)}`, {
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`Calendar HTTP ${response.status}`);
  return (await response.json()) as CalendarPayload;
}

export function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [calendarMonth, setCalendarMonth] = useState(currentMonthKey);
  const [calendarDays, setCalendarDays] = useState<CalendarDay[]>([]);
  const [calendarLoading, setCalendarLoading] = useState(true);
  const [calendarError, setCalendarError] = useState<string | null>(null);

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

  useEffect(() => {
    let cancelled = false;
    void fetchCalendar(calendarMonth)
      .then((payload) => {
        if (cancelled) return;
        setCalendarDays(payload.days);
        setCalendarError(null);
      })
      .catch((calendarRefreshError) => {
        if (cancelled) return;
        setCalendarDays([]);
        setCalendarError(
          calendarRefreshError instanceof Error
            ? calendarRefreshError.message
            : "Calendar data unavailable",
        );
      })
      .finally(() => {
        if (!cancelled) setCalendarLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [calendarMonth]);

  const shiftCalendarMonth = useCallback((offset: number) => {
    setCalendarLoading(true);
    setCalendarMonth((value) => shiftMonth(value, offset));
  }, []);

  const showCurrentCalendarMonth = useCallback(() => {
    const current = currentMonthKey();
    if (calendarMonth === current) return;
    setCalendarLoading(true);
    setCalendarMonth(current);
  }, [calendarMonth]);

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

      <CalendarPanel
        month={calendarMonth}
        days={calendarDays}
        loading={calendarLoading}
        error={calendarError}
        timezone={data.timezone}
        onPrevious={() => shiftCalendarMonth(-1)}
        onNext={() => shiftCalendarMonth(1)}
        onToday={showCurrentCalendarMonth}
      />

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

function CalendarPanel({
  month,
  days,
  loading,
  error,
  timezone,
  onPrevious,
  onNext,
  onToday,
}: {
  month: string;
  days: CalendarDay[];
  loading: boolean;
  error: string | null;
  timezone: string;
  onPrevious: () => void;
  onNext: () => void;
  onToday: () => void;
}) {
  const [year, monthNumber] = month.split("-").map(Number);
  const firstDay = new Date(year, monthNumber - 1, 1);
  const daysInMonth = new Date(year, monthNumber, 0).getDate();
  const mondayOffset = (firstDay.getDay() + 6) % 7;
  const dayByDate = new Map(days.map((day) => [day.operationalDate, day]));
  const cells = Array.from(
    { length: Math.ceil((mondayOffset + daysInMonth) / 7) * 7 },
    (_, index) => index - mondayOffset + 1,
  );
  const displayMonth = new Intl.DateTimeFormat("zh-CN", {
    timeZone: timezone,
    year: "numeric",
    month: "long",
  }).format(new Date(year, monthNumber - 1, 1));

  return (
    <section className="panel calendar-panel" aria-label="Daily collection calendar">
      <div className="calendar-header">
        <div>
          <p className="eyebrow">DAILY HISTORY</p>
          <h2>Daily collection status</h2>
          <span>Historical operating-day records · Europe/Amsterdam day boundary 04:00</span>
        </div>
        <div className="calendar-controls">
          <button type="button" onClick={onPrevious} aria-label="Previous month">‹</button>
          <strong>{displayMonth}</strong>
          <button type="button" onClick={onNext} aria-label="Next month">›</button>
          <button type="button" className="calendar-today" onClick={onToday}>Today</button>
        </div>
      </div>
      {error && <div className="calendar-note">Daily history is temporarily unavailable: {error}</div>}
      <div className="calendar-weekdays" aria-hidden="true">
        {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((weekday) => <span key={weekday}>{weekday}</span>)}
      </div>
      <div className={`calendar-grid ${loading ? "calendar-loading" : ""}`}>
        {cells.map((dayNumber, index) => {
          if (dayNumber < 1 || dayNumber > daysInMonth) {
            return <span className="calendar-cell calendar-cell-empty" key={`empty-${index}`} />;
          }
          const date = `${month}-${String(dayNumber).padStart(2, "0")}`;
          const day = dayByDate.get(date);
          const status = day?.healthStatus ?? "no_data";
          const completion = day?.completionRate == null ? "—" : percent.format(day.completionRate);
          const label = day
            ? `${date}: ${calendarStatusLabel(status)}, ${completion} completion, ${integer.format(day.received)} API items, ${integer.format(day.failedRuns)} failed requests`
            : `${date}: No daily summary`;
          return (
            <div className={`calendar-cell calendar-day calendar-${status}`} key={date} title={label}>
              <div className="calendar-day-top"><strong>{dayNumber}</strong>{day && <i aria-label={calendarStatusLabel(status)} />}</div>
              <span>{day ? calendarStatusLabel(status) : "No data"}</span>
              {day && <small>{completion} · {integer.format(day.received)} items</small>}
            </div>
          );
        })}
      </div>
      <div className="calendar-legend" aria-label="Calendar status legend">
        {(["healthy", "warning", "critical", "no_data"] as HealthStatus[]).map((status) => (
          <span key={status}><i className={`calendar-dot calendar-dot-${status}`} />{calendarStatusLabel(status)}</span>
        ))}
      </div>
    </section>
  );
}
