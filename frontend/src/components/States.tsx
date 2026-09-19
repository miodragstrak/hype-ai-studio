import { AlertCircle, LoaderCircle, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

export function LoadingState({ label = "Loading" }: { label?: string }) { return <div className="state"><LoaderCircle className="spin" aria-hidden="true"/><span>{label}</span></div>; }
export function EmptyState({ title, children }: { title: string; children?: ReactNode }) { return <div className="empty"><strong>{title}</strong>{children}</div>; }
export function ErrorState({ message, retry }: { message: string; retry?: () => void }) { return <div className="error" role="alert"><AlertCircle aria-hidden="true"/><span>{message}</span>{retry && <button className="icon-button" onClick={retry} title="Retry"><RefreshCw/><span className="sr-only">Retry</span></button>}</div>; }
