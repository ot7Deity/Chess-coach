/**
 * The dashboard's only door to data. Every page calls these three functions
 * and nothing else, so swapping fixtures for the real API is one env var:
 *
 *   NEXT_PUBLIC_API_URL=http://localhost:8000   -> the M4 API (docs/api-contract.md)
 *   unset                                       -> fixtures (./fixtures/mock.ts)
 *
 * NEXT_PUBLIC_* is inlined at build time, so a build made without it ships the
 * fixtures; the fixture module is loaded lazily, so a build made with it never
 * downloads them.
 */
import { ApiError, isAbort } from "./api-errors";
import type { ErrorBody, PlayerProgress, Report, TimeClass } from "./api-types";

export { ApiError, describeError, isAbort } from "./api-errors";

const BASE_URL = (process.env.NEXT_PUBLIC_API_URL ?? "").trim().replace(/\/+$/, "");

/** True when the dashboard is showing fixtures rather than a live API. */
export const usingFixtures = BASE_URL === "";

const USERNAME = /^[a-z0-9_-]{1,50}$/;

/** Lowercased and trimmed, or null if Chess.com could not have issued it. Same rule as the API. */
export function normalizeUsername(raw: string): string | null {
  const name = raw.trim().toLowerCase();
  return USERNAME.test(name) ? name : null;
}

/**
 * A route parameter as text. decodeURIComponent throws on a stray "%" (a URL
 * like /players/a%25), which would turn a typo into a crashed page; an
 * undecodable value is returned as-is and then fails username validation.
 */
export function decodeParam(raw: string): string {
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

const mock = () => import("./fixtures/mock");

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: { Accept: "application/json", ...init.headers },
      cache: "no-store",
    });
  } catch (err) {
    if (isAbort(err)) throw err;
    throw new ApiError(0, "Can't reach the Chess Coach server.");
  }
  if (!res.ok) {
    let message = `Request failed (${res.status}).`;
    try {
      const body = (await res.json()) as ErrorBody;
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // Not JSON (a proxy error page, say): keep the generic message.
    }
    throw new ApiError(res.status, message);
  }
  return (await res.json()) as T;
}

const enc = encodeURIComponent;

export async function getPlayer(username: string, signal?: AbortSignal): Promise<PlayerProgress> {
  if (usingFixtures) return (await mock()).getPlayer(username, signal);
  return request(`/players/${enc(username)}`, { signal });
}

export async function getReport(
  username: string,
  timeClass: TimeClass,
  signal?: AbortSignal,
): Promise<Report> {
  if (usingFixtures) return (await mock()).getReport(username, timeClass, signal);
  return request(`/players/${enc(username)}/report?time_class=${timeClass}`, { signal });
}

export async function addPlayer(username: string, signal?: AbortSignal): Promise<PlayerProgress> {
  if (usingFixtures) return (await mock()).addPlayer(username, signal);
  return request("/players", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username }),
    signal,
  });
}
