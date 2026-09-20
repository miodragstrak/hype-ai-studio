import { ArrowLeft, Film } from "lucide-react";
import { Link, NavLink, Navigate, Outlet, Route, Routes, useOutletContext, useParams } from "react-router-dom";
import { api } from "../api/client"; import { useAsync } from "../hooks/useAsync"; import type { Project } from "../types"; import { ErrorState, LoadingState } from "../components/States";
import { AssetsPage, NewProjectPage, OverviewPage, ProjectsPage, RenderPage, ReviewPage, ShotsPage } from "./pages";
import { PlanPage } from "./plan";

const stages = [["", "Setup"], ["assets", "Assets"], ["plan", "Plan"], ["shots", "Shots"], ["review", "Review"], ["render", "Render"]];
type WorkspaceContext = { project: Project; refreshProject: () => void };
export const useWorkspace = () => useOutletContext<WorkspaceContext>();
function Workspace() {
  const { projectId = "" } = useParams(); const state = useAsync(() => api.project(projectId), [projectId]);
  if (state.loading) return <AppFrame><LoadingState label="Opening project"/></AppFrame>; if (state.error || !state.data) return <AppFrame><ErrorState message={state.error || "Project unavailable"} retry={state.refresh}/></AppFrame>;
  return <AppFrame project={state.data}><nav className="stages" aria-label="Project stages">{stages.map(([path, label], i) => <NavLink key={label} end={!path} to={`/projects/${projectId}${path ? `/${path}` : ""}`}><span>{i + 1}</span>{label}</NavLink>)}</nav><Outlet context={{ project: state.data, refreshProject: state.refresh } satisfies WorkspaceContext}/></AppFrame>;
}
function AppFrame({ project, children }: { project?: Project; children: React.ReactNode }) { return <div className="app"><header className="topbar"><Link to="/projects" className="brand"><Film aria-hidden="true"/>Hype AI Studio</Link>{project && <div className="project-context"><span>Current project</span><strong>{project.title}</strong></div>}<Link to="/projects" className="back"><ArrowLeft/>Projects</Link></header><main className="workspace">{children}</main></div>; }
export function App() { return <Routes><Route path="/projects" element={<AppFrame><ProjectsPage/></AppFrame>}/><Route path="/projects/new" element={<AppFrame><NewProjectPage/></AppFrame>}/><Route path="/projects/:projectId" element={<Workspace/>}><Route index element={<OverviewPage/>}/><Route path="assets" element={<AssetsPage/>}/><Route path="plan" element={<PlanPage/>}/><Route path="shots" element={<ShotsPage/>}/><Route path="review" element={<ReviewPage/>}/><Route path="render" element={<RenderPage/>}/></Route><Route path="*" element={<Navigate to="/projects" replace/>}/></Routes>; }
