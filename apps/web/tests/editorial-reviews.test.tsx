import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { EditorialReviewDetail } from "@/components/editorial/reviews-panel";
import { editorialReviewHref, publicPostUrl } from "@/lib/editorial-reviews";
import { api } from "@/lib/api";
vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(), api: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
it("shows draft-only disposition, complete metadata, and restricted image previews", async () => {
  vi.mocked(api).mockResolvedValue({ review_id:"r2", connection_id:"c1", title:"Approved draft", revision:"sha-456", status:"approved", author_agent_id:"blogger", publisher_agent_id:"director", updated_at:"2026-09-12T00:00:00Z", html:"<p>Full article ending.</p>", url:"https://blog.example.test/draft", release_intent:"draft_only", assignment_id:"assignment-1", package_id:"package-1", revision_round:2, complete:true, html_total_chars:27, metadata:{feature_image:"https://images.unsplash.com/photo-example",feature_image_alt:"A mountain",feature_image_caption:"Photo by Example",meta_title:"Useful SEO title",authors:[{name:"Mindy"}],tags:[{name:"Guides"}]}, package:{editorial_version:3,evidence:[]} });
  render(<QueryClientProvider client={new QueryClient()}><EditorialReviewDetail workspaceId="ws" connectionId="c1" reviewId="r2" onClose={vi.fn()} /></QueryClientProvider>);
  await screen.findByText("Approved draft");
  expect(screen.getByText(/Draft only · publication disabled/)).toBeDefined();
  expect(screen.getByText(/Review round 2 · editorial version 3/)).toBeDefined();
  expect(screen.getByText(/Useful SEO title/)).toBeDefined();
  const frame = screen.getByTitle("Reviewed draft preview");
  expect(frame.getAttribute("srcdoc")).toContain("img-src data: https://images.unsplash.com");
  expect(frame.getAttribute("srcdoc")).toContain('alt="A mountain"');
  expect(frame.getAttribute("srcdoc")).toContain("Full article ending.");
  expect(screen.queryByRole("button", {name:/Publish|Approve/})).toBeNull();
});
it("inspects the exact review in a script-free isolated frame with no publish controls", async () => {
  vi.mocked(api).mockResolvedValue({ review_id:"r1", connection_id:"c1", title:"A reviewed draft", revision:"sha-123", status:"changes_requested", author_agent_id:"blogger",publisher_agent_id:"director",updated_at:"2026-09-12T00:00:00Z", feedback:"Add a source", html:"<h1>Draft</h1><script>parent.document.body.remove()</script>",url:"javascript:alert(1)" });
  render(<QueryClientProvider client={new QueryClient()}><EditorialReviewDetail workspaceId="ws" connectionId="c1" reviewId="r1" onClose={vi.fn()} /></QueryClientProvider>);
  await screen.findByText("A reviewed draft");
  expect(screen.getByText("Add a source")).toBeDefined();
  expect(screen.getByRole("link", {name:"Designated director"}).getAttribute("href")).toBe("/agents/director");
  expect(screen.getByTitle("Reviewed draft preview").getAttribute("sandbox")).toBe("");
  expect(screen.queryByRole("link", {name:"Source post"})).toBeNull();
  expect(screen.queryByRole("button", {name:/Publish|Approve|Looks good/})).toBeNull();
});
it("only builds scoped evidence links and rejects credential-bearing or executable source URLs", () => {
  expect(editorialReviewHref({kind:"ghost_editorial",connection_id:"c1",editorial_review_id:"r1"})).toBe("/editorial-reviews?connection=c1&review=r1");
  expect(editorialReviewHref({kind:"other",connection_id:"c1",editorial_review_id:"r1"})).toBeNull();
  expect(publicPostUrl("https://user:password@example.com/post")).toBeUndefined();
  expect(publicPostUrl("https://example.com/post")).toBe("https://example.com/post");
});
