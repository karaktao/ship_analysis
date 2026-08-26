import { readdir, readFile } from "node:fs/promises";
import { resolve } from "node:path";

export const runtime = "nodejs";

type CollectionSummary = {
  operational_date?: string;
  timezone?: string;
  generated_at_utc?: string;
  health_status?: string;
  collection?: {
    expected_run_count?: number;
    observed_run_count?: number;
    completed_run_count?: number;
    failed_run_count?: number;
    running_run_count?: number;
    received_item_count?: number;
    new_observation_count?: number;
    unique_item_count?: number;
    active_minute_count?: number;
    expected_minute_count?: number;
  };
};

type CalendarDay = {
  operationalDate: string;
  generatedAt: string | null;
  healthStatus: string;
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

const MONTH_PATTERN = /^(\d{4})-(0[1-9]|1[0-2])$/;
const SUMMARY_PATTERN = /^collection-summary-(\d{4}-\d{2}-\d{2})\.json$/;

function summaryRoots() {
  return [
    process.env.SHIP_ANALYSIS_SUMMARIES_DIR,
    resolve(process.cwd(), "../data/summaries"),
    resolve(process.cwd(), "data/summaries"),
    resolve(process.cwd(), "../../data/summaries"),
  ].filter((value): value is string => Boolean(value));
}

function asNumber(value: number | undefined) {
  return Number.isFinite(value) ? Number(value) : 0;
}

function toCalendarDay(summary: CollectionSummary, date: string): CalendarDay {
  const collection = summary.collection ?? {};
  const expectedRuns = asNumber(collection.expected_run_count);
  const completedRuns = asNumber(collection.completed_run_count);
  return {
    operationalDate: date,
    generatedAt: summary.generated_at_utc ?? null,
    healthStatus: summary.health_status ?? "no_data",
    received: asNumber(collection.received_item_count),
    newObservations: asNumber(collection.new_observation_count),
    uniqueItems: asNumber(collection.unique_item_count),
    expectedRuns,
    observedRuns: asNumber(collection.observed_run_count),
    completedRuns,
    failedRuns: asNumber(collection.failed_run_count),
    runningRuns: asNumber(collection.running_run_count),
    completionRate:
      expectedRuns > 0 ? completedRuns / expectedRuns : null,
    isFinal: true,
  };
}

async function readMonth(month: string): Promise<CalendarDay[]> {
  const match = MONTH_PATTERN.exec(month);
  if (!match) return [];
  const monthDirectory = `${match[1]}/${match[2]}`;

  for (const root of summaryRoots()) {
    const directory = resolve(root, monthDirectory);
    try {
      const entries = await readdir(directory, { withFileTypes: true });
      const files = entries
        .filter((entry) => entry.isFile())
        .map((entry) => entry.name)
        .map((name) => ({ name, match: SUMMARY_PATTERN.exec(name) }))
        .filter(
          (entry): entry is { name: string; match: RegExpExecArray } =>
            Boolean(entry.match),
        )
        .filter((entry) => entry.match[1].startsWith(month))
        .sort((left, right) => left.match[1].localeCompare(right.match[1]));

      const days = await Promise.all(
        files.map(async ({ name, match: fileMatch }) => {
          try {
            const content = await readFile(resolve(directory, name), "utf8");
            return toCalendarDay(JSON.parse(content) as CollectionSummary, fileMatch[1]);
          } catch {
            return null;
          }
        }),
      );
      return days.filter((day): day is CalendarDay => day !== null);
    } catch {
      // Try the next deployment layout. Missing months are valid and return no data.
    }
  }
  return [];
}

export async function GET(request: Request) {
  const month = new URL(request.url).searchParams.get("month") ?? "";
  const days = await readMonth(month);
  return Response.json(
    {
      schemaVersion: 1,
      month,
      timezone: "Europe/Amsterdam",
      days,
    },
    {
      headers: {
        "Cache-Control": "no-store",
      },
    },
  );
}
