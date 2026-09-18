import { afterEach, expect, it, vi } from "vitest";
import { waitForPreview } from "@/lib/preview-readiness";
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

it("waits for the registered app to start without opening an early gateway error page", async () => {
  vi.useFakeTimers();
  const fetchMock = vi.fn().mockResolvedValueOnce(new Response(null, { status: 502 })).mockResolvedValueOnce(new Response(null, { status: 503 })).mockResolvedValue(new Response(null, { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  const completed = vi.fn();
  const result = waitForPreview("/runtime/previews/id/ticket/", new AbortController().signal).then(completed);
  await vi.advanceTimersByTimeAsync(2_000); expect(completed).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(2_000); await result;
  expect(fetchMock).toHaveBeenCalledTimes(3); expect(fetchMock.mock.calls[0][1].method).toBe("HEAD");
});

it.each([200, 404, 500, 501])("allows the real application GET when HEAD responds %s", async (status) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status })));
  await expect(waitForPreview("/runtime/previews/id/ticket/", new AbortController().signal)).resolves.toBeUndefined();
});

it("surfaces revoked access instead of repeatedly requesting the expired ticket", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 403 })); vi.stubGlobal("fetch", fetchMock);
  await expect(waitForPreview("/runtime/previews/id/ticket/", new AbortController().signal)).rejects.toThrow("expired or was revoked");
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("cancels the pending retry when another preview is selected", async () => {
  vi.useFakeTimers(); const controller = new AbortController();
  const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 502 })); vi.stubGlobal("fetch", fetchMock);
  const result = waitForPreview("/runtime/previews/id/ticket/", controller.signal);
  const rejected = expect(result).rejects.toThrow();
  await vi.advanceTimersByTimeAsync(1); controller.abort(); await rejected;
  await vi.advanceTimersByTimeAsync(10_000); expect(fetchMock).toHaveBeenCalledTimes(1);
});
