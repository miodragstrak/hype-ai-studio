import { useAsync } from "../hooks/useAsync"; import { api } from "../api/client"; import { ErrorState, LoadingState } from "./States";
const labels: Record<string, string> = { PROJECT_CREATED: "Project created", ASSET_UPLOADED: "Asset uploaded", JOB_SUBMITTED: "Generation submitted", VARIANT_CREATED: "Variant ready", VARIANT_SELECTED: "Variant selected", VARIANT_REJECTED: "Variant rejected", RENDER_SUBMITTED: "Render submitted", RENDER_COMPLETED: "Render completed" };
export function Activity({ projectId }: { projectId: string }) {
  const state = useAsync(() => api.events(projectId), [projectId]);
  if (state.loading) return <LoadingState label="Loading activity"/>; if (state.error) return <ErrorState message={state.error} retry={state.refresh}/>;
  return <ol className="activity">{[...(state.data || [])].reverse().slice(0, 12).map((event, index) => <li key={`${event.entity_id}-${index}`}><span>{labels[event.event_type] || (event.event_type === "JOB_STATE_CHANGED" ? `Generation ${String(event.payload.to || "updated").toLowerCase()}` : event.event_type)}</span><time>{new Date(event.created_at).toLocaleString()}</time></li>)}</ol>;
}
