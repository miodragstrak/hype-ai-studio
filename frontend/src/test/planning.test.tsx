import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { api } from "../api/client";
import { App } from "../routes/App";
import type { Plan, PlanSummary, Project } from "../types";

const project: Project = { id: "p1", project_type: "MUSIC_VIDEO", title: "Night Signal", creative_brief: "A nocturnal performance in practical light", aspect_ratio: "16:9", status: "DRAFT", created_at: "x", updated_at: "x" };
const plan: Plan = { id: "plan-1", project_id: "p1", version: 1, status: "DRAFT", parent_plan_id: null, planning_inputs: { target_duration_seconds: 30, visual_tone: "cinematic", narrative_approach: "progression", performance_presence: "restrained", pacing: "measured", constraints: "", use_reference_assets: false, maximum_shot_count: 8, idempotency_key: "key" }, schema_version: "music-video-plan-v1", concept_title: "Silver Pulse", logline: "Light gathers around a solitary performance.", treatment: "A measured sequence develops from darkness into light.", creative_direction: { visual_style: "tactile", color_palette: ["silver"], camera_language: "slow movement", editing_rhythm: "measured", performance_direction: "restrained", continuity_notes: [], avoid: ["logos"] }, shots: [{ item_key: "shot-001", ordinal: 1, title: "Arrival", description: "The signal appears", prompt: "Wide cinematic stage", duration_seconds: 30, shot_type: "wide", camera: "push", subject: "performer", environment: "stage", continuity_notes: "", reference_asset_ids: [] }], provider: "mock", model: "mock-music-video-planner-v1", source_job_id: "job-1", created_at: "x", approved_at: null, materialized_at: null };
const summary = (value: Plan): PlanSummary => ({ id: value.id, version: value.version, status: value.status, concept_title: value.concept_title, provider: value.provider, model: value.model, created_at: value.created_at, approved_at: value.approved_at });

it("generates, polls, edits, versions, and approves a plan", async () => {
  let plans: PlanSummary[] = []; let current = plan;
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(api, "project").mockResolvedValue(project); vi.spyOn(api, "plans").mockImplementation(async () => plans); vi.spyOn(api, "plan").mockImplementation(async () => current);
  vi.spyOn(api, "generatePlan").mockImplementation(async () => { plans = [summary(plan)]; return { job_id: "job-1", status: "QUEUED", deduplicated: false }; });
  vi.spyOn(api, "job").mockResolvedValue({ id: "job-1", status: "SUCCEEDED", retry_count: 0, error_data: null, created_at: "x", started_at: "x", completed_at: "x" });
  vi.spyOn(api, "createPlanVersion").mockImplementation(async (_project, parent, body) => { current = { ...plan, ...body, id: "plan-2", version: 2, parent_plan_id: parent, provider: "producer", model: "manual-edit" }; plans = [summary(current), summary(plan)]; return current; });
  vi.spyOn(api, "approvePlan").mockImplementation(async (_project, id) => { current = { ...current, status: "APPROVED" }; plans = [summary(current), summary(plan)]; return { plan_id: id, status: "APPROVED", created_shot_ids: ["shot-1"], idempotent: false }; });

  render(<MemoryRouter initialEntries={["/projects/p1/plan"]}><App/></MemoryRouter>);
  expect(await screen.findByText("No plan yet")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "Generate plan" }));
  expect(await screen.findByDisplayValue("Silver Pulse")).toBeVisible();
  await userEvent.clear(screen.getByLabelText("Concept title")); await userEvent.type(screen.getByLabelText("Concept title"), "Producer Cut");
  await userEvent.click(screen.getByRole("button", { name: "Save as new version" }));
  await waitFor(() => expect(api.createPlanVersion).toHaveBeenCalled());
  expect((await screen.findAllByText("Version 2")).length).toBeGreaterThan(0);
  await userEvent.click(screen.getByRole("button", { name: "Approve plan" }));
  await waitFor(() => expect(api.approvePlan).toHaveBeenCalledWith("p1", "plan-2"));
  expect((await screen.findAllByText("Approved")).length).toBeGreaterThan(0);
});

it("shows validation feedback and blocks approval outside duration tolerance", async () => {
  vi.spyOn(api, "project").mockResolvedValue(project); vi.spyOn(api, "plans").mockResolvedValue([summary(plan)]); vi.spyOn(api, "plan").mockResolvedValue(plan);
  render(<MemoryRouter initialEntries={["/projects/p1/plan"]}><App/></MemoryRouter>);
  const duration = await screen.findByLabelText("Duration"); await userEvent.clear(duration); await userEvent.type(duration, "10");
  expect(screen.getByText(/Duration must be within 10%/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Approve plan" })).toBeDisabled();
});
