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
  /** Machine-readable code when the API sent `{code, message}` (media,
   * coordination). Null for plain `{detail}` errors. */
  readonly code: string | null;

  constructor(status: number, detail: string, code: string | null = null) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.code = code;
  }
}

function readCookie(name: string): string | null {
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

/** `{code}` at the top level or nested under `detail` (FastAPI HTTPException
 * with a dict detail). */
function extractCode(payload: unknown): string | null {
  if (typeof payload !== "object" || payload === null) return null;
  const record = payload as Record<string, unknown>;
  if (typeof record.code === "string") return record.code;
  const detail = record.detail;
  if (typeof detail === "object" && detail !== null) {
    const code = (detail as Record<string, unknown>).code;
    if (typeof code === "string") return code;
  }
  return null;
}

function extractDetail(payload: unknown): string | null {
  if (typeof payload !== "object" || payload === null) return null;
  const record = payload as Record<string, unknown>;
  const detail = record.detail;
  if (typeof detail === "string") return detail;
  if (typeof detail === "object" && detail !== null && !Array.isArray(detail)) {
    const message = (detail as Record<string, unknown>).message;
    if (typeof message === "string") return message;
  }
  if (typeof record.message === "string") return record.message;
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
      extractCode(payload),
    );
  }
  return payload as T;
}

/** Multipart upload (avatars). Sends the CSRF header; the browser sets the
 * multipart boundary itself, so no content-type is set here. */
export async function apiUpload<T>(path: string, formData: FormData): Promise<T> {
  const headers: Record<string, string> = {};
  const csrf = readCookie(CSRF_COOKIE);
  if (csrf) headers[CSRF_HEADER] = csrf;

  const response = await fetch(path, { method: "POST", headers, body: formData });
  if (response.status === 204) return undefined as T;

  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    // non-JSON body
  }
  if (!response.ok) {
    throw new ApiError(
      response.status,
      extractDetail(payload) ?? `Upload failed (${response.status})`,
      extractCode(payload),
    );
  }
  return payload as T;
}
