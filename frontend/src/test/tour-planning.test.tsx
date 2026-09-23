import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { api } from "../api/client";
import { App } from "../routes/App";
import type { Plan, PlanShot, PlanSummary, Project } from "../types";

const project: Project = { id: "tour-1", project_type: "TOUR_GUIDE", title: "Beograd POV", creative_brief: "Beograd, vertikalni 9:16, POV putopis na srpskom jeziku", aspect_ratio: "9:16", status: "DRAFT", created_at: "x", updated_at: "x" };
const scene = (ordinal: number): PlanShot => ({ item_key: `scene-${ordinal}`, ordinal, title: ordinal === 1 ? "Uvodni naslov" : ordinal === 4 ? "Završni kadar" : `Lokacija ${ordinal}`, description: "POV scena", prompt: "Vertical POV", duration_seconds: 10, shot_type: "POV", camera: "handheld", subject: "travel", environment: "Belgrade", continuity_notes: "", reference_asset_ids: [], location_or_motif: `Motiv ${ordinal}`, pov_description: "Kretanje iz prvog lica", narration: `Kratka naracija ${ordinal}`, factual_claims: ordinal === 2 ? [{ claim: "Proverljiva tvrdnja", sources: [] }] : [] });
const plan: Plan = { id: "plan-1", project_id: project.id, version: 1, status: "DRAFT", parent_plan_id: null, planning_inputs: { project_type: "TOUR_GUIDE", target_duration_seconds: 40, visual_tone: "urban", narrative_approach: "POV", performance_presence: "host", pacing: "brisk", constraints: "Serbian", use_reference_assets: false, maximum_shot_count: 5, idempotency_key: "key" }, schema_version: "tour-guide-plan-v1", concept_title: "Beograd iz prvog lica", logline: "Kratak putopis", treatment: "Mock plan sa proverom izvora.", creative_direction: { visual_style: "urban", color_palette: ["stone"], camera_language: "POV", editing_rhythm: "brisk", performance_direction: "Serbian narration", continuity_notes: [], avoid: ["unverified facts"] }, shots: [1, 2, 3, 4].map(scene), provider: "mock", model: "mock-tour-guide-planner-v1", source_job_id: "job-1", created_at: "x", approved_at: null, materialized_at: null };
const summary = (value: Plan): PlanSummary => ({ id: value.id, version: value.version, status: value.status, concept_title: value.concept_title, provider: value.provider, model: value.model, created_at: value.created_at, approved_at: value.approved_at });

it("blocks an unsourced tour claim and approves the sourced immutable version", async () => {
  let current = plan; let history = [summary(plan)];
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(api, "project").mockResolvedValue(project);
  vi.spyOn(api, "plans").mockImplementation(async () => history);
  vi.spyOn(api, "plan").mockImplementation(async () => current);
  vi.spyOn(api, "createPlanVersion").mockImplementation(async (_project, parent, body) => { current = { ...plan, ...body, id: "plan-2", version: 2, parent_plan_id: parent, provider: "producer", model: "manual-edit" }; history = [summary(current), summary(plan)]; return current; });
  vi.spyOn(api, "approvePlan").mockImplementation(async (_project, id) => { current = { ...current, status: "APPROVED" }; history = [summary(current), summary(plan)]; return { plan_id: id, status: "APPROVED", created_shot_ids: [], idempotent: false }; });
  vi.spyOn(api, "handoffTourPlan").mockResolvedValue({ plan_id: "plan-2", status: "MATERIALIZED", created_shot_ids: ["s1", "s2", "s3", "s4"], idempotent: false });

  render(<MemoryRouter initialEntries={["/projects/tour-1/plan"]}><App/></MemoryRouter>);
  expect(await screen.findByText("Tour scenario")).toBeVisible();
  expect(await screen.findByText("Missing source — approval blocked")).toBeVisible();
  expect(screen.getByRole("button", { name: "Approve plan" })).toBeDisabled();
  expect(screen.getByRole("link", { name: /Shots$/ })).toBeVisible();

  await userEvent.type(screen.getByLabelText("Sources for scene 2 claim 1"), "https://example.test/source");
  expect(screen.getByText("Source supplied")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "Approve plan" }));
  await waitFor(() => expect(api.createPlanVersion).toHaveBeenCalledWith("tour-1", "plan-1", expect.objectContaining({ schema_version: "tour-guide-plan-v1" })));
  await waitFor(() => expect(api.approvePlan).toHaveBeenCalledWith("tour-1", "plan-2"));
  expect(await screen.findByText(/Approved immutable version 2/)).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "Create production shots" }));
  await waitFor(() => expect(api.handoffTourPlan).toHaveBeenCalledWith("tour-1", "plan-2"));
  expect(await screen.findByRole("link", { name: "Open 4 shots" })).toBeVisible();
});
