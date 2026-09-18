"use client";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { Badge, Button, Dialog, ErrorNote, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { editorialBase, editorialPreview, publicPostUrl, type EditorialReview } from "@/lib/editorial-reviews";

export function EditorialReviewDetail({ workspaceId, connectionId, reviewId, onClose }: { workspaceId: string; connectionId: string; reviewId: string; onClose: () => void }) {
  const query = useQuery({ queryKey: ["editorial-review", workspaceId, connectionId, reviewId], queryFn: () => api<EditorialReview>(`${editorialBase(workspaceId, connectionId)}/${reviewId}`), refetchInterval: 15000 });
  const item = query.data, sourceUrl = item ? publicPostUrl(item.url) : undefined;
  const html = item?.html ?? "";
  return <Dialog open wide title="Director review" onClose={onClose} footer={<div className="flex justify-end gap-2"><Button onClick={() => void query.refetch()}>Refresh status</Button><Button onClick={onClose}>Done</Button></div>}>
    {query.isPending ? <Spinner /> : null}<ErrorNote message={query.isError ? "Couldn't load this review. It may no longer be available to you." : null} />
    {item ? <div className="space-y-4"><div className="flex flex-wrap items-start justify-between gap-3"><h3 className="break-words font-display text-lg font-semibold">{item.title || "Untitled draft"}</h3><Badge tone={item.status === "published" ? "ok" : item.status === "uncertain" ? "warn" : "neutral"}>{item.status.replaceAll("_", " ")}</Badge></div><p className="text-sm text-dim">Only the designated director can approve and publish this exact revision. Later changes require another review.</p><p className="break-all text-xs text-dim">Revision {item.revision} · Updated {formatDateTime(item.updated_at)}</p><div className="flex flex-wrap gap-3 text-sm"><Link className="text-accent-strong underline" href={`/agents/${item.publisher_agent_id}`}>Designated director</Link><Link className="text-accent-strong underline" href={`/agents/${item.author_agent_id}`}>Author</Link>{sourceUrl ? <a className="text-accent-strong underline" href={sourceUrl} target="_blank" rel="noopener noreferrer">Source post</a> : null}</div>{item.feedback ? <section><h4 className="text-sm font-medium">Review feedback</h4><p className="mt-1 whitespace-pre-wrap break-words text-sm">{item.feedback}</p></section> : null}
      <section className="rounded-xl border border-line bg-raised p-3 text-sm" aria-label="Editorial release intent">
        <p className="font-medium">{item.release_intent === "publish_after_ashley_review" ? "Publication permitted after the designated director's exact-version review" : "Draft only · publication disabled"}</p>
        <p className="mt-1 text-dim">Review round {item.revision_round ?? 1} · editorial version {typeof item.package?.editorial_version === "number" ? item.package.editorial_version : "unbound"}</p>
        {!item.assignment_id ? <p className="mt-1 text-dim">Historical review: a new assignment and review are required before publication.</p> : null}
        {item.complete === false ? <p role="alert" className="mt-2 text-warn">This saved preview is incomplete. Retrieve a complete review before approving.</p> : <p className="mt-1 text-dim">{item.html_total_chars ?? html.length} article characters in the saved review.</p>}
      </section>
      <iframe title="Reviewed draft preview" sandbox="" referrerPolicy="no-referrer" className="h-96 w-full rounded-xl border border-line bg-white" srcDoc={editorialPreview(item)} />
      <details><summary className="cursor-pointer text-sm text-accent-strong">Images, authors, tags, and SEO metadata</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-xl bg-raised p-3 text-xs">{JSON.stringify(item.metadata ?? {}, null, 2)}</pre></details>
      {item.package ? <details><summary className="cursor-pointer text-sm text-accent-strong">Brief and source evidence</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-xl bg-raised p-3 text-xs">{JSON.stringify(item.package, null, 2)}</pre></details> : null}
      <details><summary className="cursor-pointer text-sm text-accent-strong">Inspect saved HTML</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-xl bg-raised p-3 text-xs">{html}</pre></details>
    </div> : null}
  </Dialog>;
}
export function EditorialReviewsPanel({ workspaceId, connectionId, initialReviewId }: { workspaceId: string; connectionId: string; initialReviewId?: string | null }) {
  const [selected, setSelected] = useState<string | null>(initialReviewId ?? null);
  const query = useInfiniteQuery({ queryKey: ["editorial-reviews", workspaceId, connectionId], initialPageParam: undefined as string | undefined, queryFn: ({pageParam}) => api<{items:EditorialReview[];next_before_id:string|null}>(editorialBase(workspaceId, connectionId), {params:{limit:50,before_id:pageParam}}), getNextPageParam:(page) => page.next_before_id ?? undefined, refetchInterval:15000 });
  const items = query.data?.pages.flatMap((page) => page.items) ?? [];
  return <section className="space-y-4" aria-label="Director reviews"><ErrorNote message={query.isError ? "Couldn't load director reviews." : null} />{query.isPending ? <Spinner label="Loading director reviews…" /> : null}{!query.isPending && !items.length ? <p className="rounded-xl border border-dashed border-line p-5 text-sm text-dim">No drafts submitted for director review on this connection yet.</p> : null}<ul className="space-y-3">{items.map((item) => <li key={item.review_id} className="min-w-0 rounded-xl border border-line bg-surface p-4"><div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><h3 className="break-words font-medium">{item.title || "Untitled draft"}</h3><p className="mt-1 break-all text-xs text-dim">Revision {item.revision}</p></div><Badge>{item.status.replaceAll("_", " ")}</Badge></div><div className="mt-3 flex flex-wrap items-center justify-between gap-2"><p className="text-xs text-dim">Updated {formatDateTime(item.updated_at)}</p><Button size="sm" onClick={() => setSelected(item.review_id)}>Inspect review</Button></div></li>)}</ul>{query.hasNextPage ? <Button disabled={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>Earlier reviews</Button> : null}{selected ? <EditorialReviewDetail workspaceId={workspaceId} connectionId={connectionId} reviewId={selected} onClose={() => setSelected(null)} /> : null}</section>;
}
