/** A signed report's numbers in the app's words (moved from DetailsView). */
import { fmtSeconds, fmtUsd } from './viz';

/** The signed report's fields an agent page shows, in the app's words. */
const REPORT_ROWS: Array<[string, string, (v: unknown) => string]> = [
  ['floor_score', 'Exam score', (v) => (typeof v === 'number' ? v.toFixed(2) : String(v))],
  ['floor_limit', 'Bar', (v) => (typeof v === 'number' ? v.toFixed(2) : String(v))],
  ['held_out_count', 'Exam cases', String],
  ['mean_cost_usd', 'Mean cost per run', fmtUsd],
  ['cost_usd', 'Highest cost per run', fmtUsd],
  ['compute_s', 'Longest run', fmtSeconds],
  ['component_count', 'Components', String],
];

export function rowsOf(source: Record<string, unknown>, table: Array<[string, string, (v: unknown) => string]>): Array<[string, string]> {
  return table.filter(([key]) => source[key] !== undefined && source[key] !== null).map(([key, label, show]) => [label, show(source[key])]);
}

/** A signed report's numbers in the app's words (here and on Your agents). */
export function reportRows(report: Record<string, unknown>): Array<[string, string]> {
  return rowsOf(report, REPORT_ROWS);
}
