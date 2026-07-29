export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail?: string;
  instance?: string;
  trace_id?: string;
  errors?: Array<{ pointer: string; detail: string }>;
}

export const PROBLEM_CONTENT_TYPE = "application/problem+json";

/** Narrow an unknown body to ProblemDetails without trusting the server blindly. */
export function isProblemDetails(v: unknown): v is ProblemDetails {
  return (
    typeof v === "object" &&
    v !== null &&
    typeof (v as ProblemDetails).type === "string" &&
    typeof (v as ProblemDetails).title === "string" &&
    typeof (v as ProblemDetails).status === "number"
  );
}
