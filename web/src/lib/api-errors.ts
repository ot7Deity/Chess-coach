/**
 * The one error type the data layer throws. `status` is the HTTP status, or 0
 * when the server could not be reached at all, so the UI can tell "this player
 * does not exist" (404) from "the API is down" (0 / 5xx) and word them differently.
 */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }
}

/** One sentence a player can act on. Server-side detail is kept only when it is ours (4xx). */
export function describeError(err: unknown): string {
  if (!(err instanceof ApiError)) return "Something unexpected happened. Try again.";
  if (err.status === 0) return "Can't reach the Chess Coach server. Check your connection and try again.";
  if (err.status >= 500) return `The Chess Coach server had a problem (${err.status}). Try again in a moment.`;
  return err.message;
}

export function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}
