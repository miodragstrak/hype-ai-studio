import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Job, Render } from "../types";

const JOB_TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"]);
const RENDER_TERMINAL = new Set(["SUCCEEDED", "FAILED"]);

export function useJobPolling(jobId: string | null, interval = 800) {
  const [result, setResult] = useState<{ id: string; value: Job } | null>(null);
  useEffect(() => {
    if (!jobId) return;
    let active = true;
    const poll = async () => {
      const next = await api.job(jobId);
      if (!active) return;
      setResult({ id: jobId, value: next });
      if (JOB_TERMINAL.has(next.status)) clearInterval(timer);
    };
    const timer = setInterval(() => void poll(), interval);
    void poll();
    return () => { active = false; clearInterval(timer); };
  }, [jobId, interval]);
  return result?.id === jobId ? result.value : null;
}

export function useRenderPolling(renderId: string | null, interval = 800) {
  const [result, setResult] = useState<{ id: string; value: Render } | null>(null);
  useEffect(() => {
    if (!renderId) return;
    let active = true;
    const poll = async () => {
      const next = await api.render(renderId);
      if (!active) return;
      setResult({ id: renderId, value: next });
      if (RENDER_TERMINAL.has(next.status)) clearInterval(timer);
    };
    const timer = setInterval(() => void poll(), interval);
    void poll();
    return () => { active = false; clearInterval(timer); };
  }, [renderId, interval]);
  return result?.id === renderId ? result.value : null;
}
