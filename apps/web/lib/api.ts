/**
 * Browser API client. Calls go same-origin to /api/* (proxied to the API by a
 * Next.js rewrite) so the HttpOnly session cookie flows automatically.
 * Mutating requests attach the CSRF double-submit header from the readable
 * CSRF cookie set at login.
 */

const CSRF_COOKIE = "jhin_csrf";
const CSRF_HEADER = "x-csrf-token";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  for (const part of document.cookie.split("; ")) {
    const eq = part.indexOf("=");
    if (eq > 0 && part.slice(0, eq) === name) {
      return decodeURIComponent(part.slice(eq + 1));
    }
  }
  return null;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  params?: Record<string, string | number | undefined>;
}

function extractDetail(payload: unknown): string | null {
  if (typeof payload !== "object" || payload === null) return null;
  const detail = (payload as Record<string, unknown>).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    // FastAPI validation errors: [{loc, msg, type}, ...]
    const first = detail[0] as Record<string, unknown>;
    if (typeof first.msg === "string") {
      const loc = Array.isArray(first.loc) ? first.loc.slice(1).join(".") : "";
      return loc ? `${loc}: ${first.msg}` : first.msg;
    }
  }
  return null;
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  let url = path;
  if (options.params) {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(options.params)) {
      if (value !== undefined && value !== "") search.set(key, String(value));
    }
    const query = search.toString();
    if (query) url += `?${query}`;
  }

  const headers: Record<string, string> = {};
  if (options.body !== undefined) headers["content-type"] = "application/json";
  if (method !== "GET") {
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf) headers[CSRF_HEADER] = csrf;
  }

  const response = await fetch(url, {
    method,
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });

  if (response.status === 204) return undefined as T;

  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    // non-JSON error body; fall through with generic detail
  }

  if (!response.ok) {
    throw new ApiError(
      response.status,
      extractDetail(payload) ?? `Request failed (${response.status})`,
    );
  }
  return payload as T;
}
