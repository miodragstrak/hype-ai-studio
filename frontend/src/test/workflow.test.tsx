import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { api } from "../api/client";
import { App } from "../routes/App";
import type { Asset, Project, Shot, Variant } from "../types";

it("creates, uploads, generates, reviews, and renders through real routes", async () => {
  const project: Project = { id: "p1", project_type: "MUSIC_VIDEO", title: "Launch Film", creative_brief: "A performance-led launch film", aspect_ratio: "16:9", status: "DRAFT", created_at: "x", updated_at: "x", summary: { assets: 0, shots: 0, selected_variants: 0, renders: 0 } };
  const audio: Asset = { id: "audio", asset_type: "AUDIO", filename: "song.wav", mime_type: "audio/wav", size_bytes: 100, checksum: "x", rights_metadata: { usage_confirmed: true }, created_at: "x" };
  let assets: Asset[] = []; let shots: Shot[] = []; let selected = false;
  const variant: Variant = { id: "v1", mime_type: "video/mp4", duration: 3, review_status: "UNREVIEWED", created_at: "x", generation_attempt_id: "a1", attempt_number: 1, job_id: "j1", provider: "mock", model: "mock-video-v1", generation_mode: "text-to-video" };
  vi.spyOn(api, "createProject").mockResolvedValue(project); vi.spyOn(api, "project").mockResolvedValue(project); vi.spyOn(api, "events").mockResolvedValue([]);
  vi.spyOn(api, "assets").mockImplementation(async () => assets); vi.spyOn(api, "uploadAsset").mockImplementation(async () => { assets = [audio]; return { id: "audio" }; });
  vi.spyOn(api, "shots").mockImplementation(async () => shots.map(shot => selected ? { ...shot, selected_variant_id: "v1", latest_job_status: "SUCCEEDED" } : shot));
  vi.spyOn(api, "createShot").mockImplementation(async (_id, body) => { const shot: Shot = { id: "s1", ...body, status: "PLANNED", selected_variant_id: null }; shots = [shot]; return shot; });
  vi.spyOn(api, "generate").mockResolvedValue({ job_id: "j1", status: "QUEUED" }); vi.spyOn(api, "job").mockResolvedValue({ id: "j1", status: "SUCCEEDED", retry_count: 0, error_data: null, created_at: "x", started_at: "x", completed_at: "x" });
  vi.spyOn(api, "variants").mockImplementation(async () => [{ ...variant, review_status: selected ? "SELECTED" : "UNREVIEWED" }]); vi.spyOn(api, "selectVariant").mockImplementation(async () => { selected = true; return {}; });
  vi.spyOn(api, "renders").mockResolvedValue([]); vi.spyOn(api, "submitRender").mockResolvedValue({ id: "r1", status: "QUEUED", output_storage_key: null, mime_type: null, duration: null, error_data: null, created_at: "x", started_at: null, completed_at: null }); vi.spyOn(api, "render").mockResolvedValue({ id: "r1", status: "SUCCEEDED", output_storage_key: "safe", mime_type: "video/mp4", duration: 3, error_data: null, created_at: "x", started_at: "x", completed_at: "x" });

  render(<MemoryRouter initialEntries={["/projects/new"]}><App/></MemoryRouter>);
  await userEvent.type(screen.getByLabelText("Project title"), project.title); await userEvent.type(screen.getByLabelText("Creative brief"), project.creative_brief); await userEvent.click(screen.getByRole("button", { name: "Create project" }));
  expect(await screen.findByText("Project details")).toBeVisible(); await userEvent.click(screen.getByRole("link", { name: /Assets/ }));
  await userEvent.upload(await screen.findByLabelText("AUDIO file"), new File(["audio"], "song.wav", { type: "audio/wav" })); await userEvent.click(screen.getAllByLabelText("Usage confirmed")[0]); await userEvent.click(screen.getAllByRole("button", { name: "Upload" })[0]); await waitFor(() => expect(api.uploadAsset).toHaveBeenCalledOnce());
  await userEvent.click(screen.getByRole("link", { name: /Shots/ })); await userEvent.type(await screen.findByLabelText("Title"), "Opening"); await userEvent.type(screen.getByLabelText("Generation prompt"), "Artist on stage"); await userEvent.click(screen.getByRole("button", { name: "Add shot" }));
  const generate = await screen.findByRole("button", { name: "Generate variant" }); await userEvent.click(generate); expect(await screen.findByText("Succeeded")).toBeVisible();
  await userEvent.click(screen.getByRole("link", { name: /Review/ })); await userEvent.click(await screen.findByRole("button", { name: "Select" })); await waitFor(() => expect(screen.getAllByText("Selected").length).toBeGreaterThan(0));
  await userEvent.click(screen.getByRole("link", { name: /Render/ })); const submit = await screen.findByRole("button", { name: "Submit final render" }); expect(submit).toBeEnabled(); await userEvent.click(submit); expect(await screen.findByText("Final video ready")).toBeVisible(); expect(screen.getByRole("link", { name: "Download MP4" })).toBeVisible();
});
