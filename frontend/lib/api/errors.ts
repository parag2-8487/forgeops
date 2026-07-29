import { ProblemDetails } from "./problem";

/**
 * Thrown when the server returns a non-2xx response.
 * Always wraps a ProblemDetails — either parsed from the response body
 * or synthesised when the body is non-conforming.
 */
export class ApiProblemError extends Error {
  constructor(readonly problem: ProblemDetails) {
    super(`${problem.title} (${problem.status})`);
    this.name = "ApiProblemError";
  }

  /** Field-level messages keyed by JSON pointer path, for React Hook Form setError. */
  get fieldErrors(): Record<string, string> {
    return Object.fromEntries(
      (this.problem.errors ?? []).map((e) => [e.pointer.replace(/^#\//, ""), e.detail]),
    );
  }
}

/**
 * Thrown when the network fails or the response is unparseable.
 * Synthesised into the same Problem shape so callers only ever handle one error type.
 */
export class ApiTransportError extends ApiProblemError {
  constructor(problem: ProblemDetails) {
    super(problem);
    this.name = "ApiTransportError";
  }
}
