import { useCallback, useEffect, useState } from "react";

export function useAsync<T>(load: () => Promise<T>, dependencies: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(() => {
    let active = true;
    setLoading(true);
    setError(null);
    load()
      .then((value) => { if (active) setData(value); })
      .catch((reason: Error) => { if (active) setError(reason.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  // Call sites supply stable primitive dependencies for their loader closure.
  // eslint-disable-next-line react-hooks/use-memo, react-hooks/exhaustive-deps
  }, dependencies);
  useEffect(() => refresh(), [refresh]);
  return { data, setData, loading, error, refresh };
}
