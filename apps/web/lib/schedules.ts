export interface WorkSchedule {
  id: string; workspace_id: string; name: string; agent_id: string; brief: string;
  local_time: string; timezone: string; weekdays: number[]; enabled: boolean; deleted_at: string | null;
  overlap_policy: "skip"; version: number; next_run_at: string | null; last_run_at: string | null;
  last_status: string | null; created_at: string; updated_at: string;
}
export interface ScheduleOccurrence { id: string; schedule_id: string; scheduled_for: string; status: string; task_id: string | null; started_at: string | null; finished_at: string | null; error_code: string | null; }
export const scheduleBase = (workspaceId: string) => `/api/v1/workspaces/${workspaceId}/schedules`;
export const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
export function scheduleTime(iso: string | null, timezone: string): string {
  if (!iso) return "Not scheduled";
  try { return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short", timeZone: timezone }).format(new Date(iso)); }
  catch { return iso; }
}
