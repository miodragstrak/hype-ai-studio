import type { ActivityEvent, Asset, IntroCard, Job, OutroCard, Plan, PlanGenerateInput, PlanSummary, Project, Render, Shot, TourTimeline, Variant } from "../types";

export const API_BASE = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");
export class ApiError extends Error { constructor(public status: number, message: string) { super(message); } }
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try { message = (await response.json()).detail || message; } catch { /* fallback */ }
    throw new ApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}
const json = (method: string, body: unknown): RequestInit => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const api = {
  projects: () => request<Project[]>("/projects"), project: (id: string) => request<Project>(`/projects/${id}`),
  createProject: (body: { title: string; creative_brief: string; project_type?: Project["project_type"]; aspect_ratio?: string }) => request<Project>("/projects", json("POST", { project_type: "MUSIC_VIDEO", aspect_ratio: "16:9", ...body })),
  assets: (id: string) => request<Asset[]>(`/projects/${id}/assets`), uploadAsset: (id: string, data: FormData) => request<{ id: string }>(`/projects/${id}/assets`, { method: "POST", body: data }),
  uploadVoiceover: (shotId: string, file: File) => { const data = new FormData(); data.append("file", file); return request<{ id: string; shot_id: string; duration: number; mime_type: string }>(`/shots/${shotId}/voiceover`, { method: "POST", body: data }); },
  uploadTourMusic: (projectId: string, file: File, rightsMetadata: Record<string, unknown>) => { const data = new FormData(); data.append("file", file); data.append("rights_metadata", JSON.stringify(rightsMetadata)); return request<{ id: string; duration: number; mime_type: string }>(`/projects/${projectId}/tour-music`, { method: "POST", body: data }); },
  tourTimeline: (projectId: string) => request<TourTimeline>(`/projects/${projectId}/tour-timeline`),
  plans: (id: string) => request<PlanSummary[]>(`/projects/${id}/plans`),
  plan: (projectId: string, planId: string) => request<Plan>(`/projects/${projectId}/plans/${planId}`),
  generatePlan: (id: string, body: PlanGenerateInput) => request<{ job_id: string; status: string; deduplicated: boolean }>(`/projects/${id}/plans/generate`, json("POST", body)),
  createPlanVersion: (projectId: string, planId: string, body: Pick<Plan, "schema_version" | "concept_title" | "logline" | "treatment" | "creative_direction" | "shots">) => request<Plan>(`/projects/${projectId}/plans/${planId}/versions`, json("POST", body)),
  approvePlan: (projectId: string, planId: string) => request<{ plan_id: string; status: string; created_shot_ids: string[]; idempotent: boolean }>(`/projects/${projectId}/plans/${planId}/approve`, { method: "POST" }),
  handoffTourPlan: (projectId: string, planId: string) => request<{ plan_id: string; status: string; created_shot_ids: string[]; idempotent: boolean }>(`/projects/${projectId}/plans/${planId}/handoff`, { method: "POST" }),
  shots: (id: string) => request<Shot[]>(`/projects/${id}/shots`), createShot: (id: string, body: { ordinal: number; title: string; prompt: string; intended_duration: number }) => request<Shot>(`/projects/${id}/shots`, json("POST", body)),
  updateShot: (id: string, body: Pick<Shot, "ordinal" | "title" | "prompt" | "intended_duration">) => request<Shot>(`/shots/${id}`, json("PUT", body)),
  reorderShots: (id: string, ids: string[]) => request(`/projects/${id}/shots/order`, json("PUT", { shot_ids: ids })),
  generate: (id: string, key: string, referenceAssetId?: string) => {
    const query = new URLSearchParams({ idempotency_key: key });
    if (referenceAssetId) query.set("reference_asset_id", referenceAssetId);
    return request<{ job_id: string; status: string }>(`/shots/${id}/generations?${query}`, { method: "POST" });
  },
  job: (id: string) => request<Job>(`/jobs/${id}`), variants: (id: string) => request<Variant[]>(`/shots/${id}/variants`),
  selectVariant: (shot: string, variant: string) => request(`/shots/${shot}/variants/${variant}/select`, { method: "POST" }),
  rejectVariant: (shot: string, variant: string) => request(`/shots/${shot}/variants/${variant}/reject`, { method: "POST" }),
  renders: (id: string) => request<Render[]>(`/projects/${id}/renders`), submitRender: (id: string, variants: string[], audio: string, audioStartSeconds = 0, cards: { intro_card?: IntroCard; outro_card?: OutroCard } = {}) => request<Render>(`/projects/${id}/renders`, json("POST", { variant_ids: variants, audio_asset_id: audio, audio_start_seconds: audioStartSeconds, ...cards })),
  submitTourRender: (id: string, body: { opening_title: string; closing_title: string; music_asset_id: string | null }) => request<Render>(`/projects/${id}/tour-renders`, json("POST", body)),
  render: (id: string) => request<Render>(`/renders/${id}`), events: (id: string) => request<ActivityEvent[]>(`/projects/${id}/events`),
  media: (kind: "assets" | "variants" | "renders", id: string, download = false) => `${API_BASE}/${kind}/${id}/media${download ? "?download=true" : ""}`,
};
