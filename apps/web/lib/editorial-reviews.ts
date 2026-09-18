export interface EditorialReview {
  review_id: string; connection_id: string; post_id: string; revision: string; status: string;
  publisher_agent_id: string; author_agent_id: string; title: string; html?: string | null;
  provider_updated_at: string; feedback: string; url: string; work_request_id: string | null;
  created_at: string; updated_at: string; decided_at: string | null; published_at: string | null;
  assignment_id?: string | null; package_id?: string | null; release_intent?: "draft_only" | "publish_after_ashley_review";
  revision_round?: number; complete?: boolean; html_total_chars?: number;
  metadata?: Record<string, unknown>; package?: Record<string, unknown> | null;
}

const escapeHtml = (value: unknown) => String(value ?? "").replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
export function editorialPreview(item: EditorialReview): string {
  const metadata = item.metadata ?? {};
  const image = typeof metadata.feature_image === "string" ? publicPostUrl(metadata.feature_image) : undefined;
  const cover = image && new URL(image).origin === "https://images.unsplash.com"
    ? `<figure><img src="${escapeHtml(image)}" alt="${escapeHtml(metadata.feature_image_alt)}"><figcaption>${String(metadata.feature_image_caption ?? "")}</figcaption></figure>` : "";
  return `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data: https://images.unsplash.com; base-uri 'none'; form-action 'none'"><style>body{font:16px system-ui;line-height:1.6;padding:20px;overflow-wrap:anywhere}img{max-width:100%;height:auto}figure{margin:0}figcaption{font-size:13px;color:#555}</style></head><body>${cover}${item.html ?? ""}</body></html>`;
}
export const editorialBase = (workspaceId: string, connectionId: string) => `/api/v1/workspaces/${workspaceId}/connections/${connectionId}/editorial-reviews`;
export function editorialReviewHref(evidence: Record<string, unknown>): string | null {
  return evidence.kind === "ghost_editorial" && typeof evidence.connection_id === "string" && typeof evidence.editorial_review_id === "string" ? `/editorial-reviews?connection=${encodeURIComponent(evidence.connection_id)}&review=${encodeURIComponent(evidence.editorial_review_id)}` : null;
}
export function publicPostUrl(value: string): string | undefined {
  try { const url = new URL(value); return ["http:","https:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined; } catch { return undefined; }
}
