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

export function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}
