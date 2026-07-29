import type { FieldPath, FieldValues, UseFormSetError } from "react-hook-form";
import { ApiProblemError } from "@/lib/api";

/**
 * Maps ApiProblemError.fieldErrors (JSON pointer paths) onto React Hook Form setError.
 * Converts JSON pointer segments like "address/city" to dot notation "address.city"
 * for RHF compatibility.
 */
export function applyFieldErrors<T extends FieldValues>(
  error: ApiProblemError,
  setError: UseFormSetError<T>,
): void {
  const fieldErrors = error.fieldErrors;
  for (const [pointer, message] of Object.entries(fieldErrors)) {
    // Convert slash-separated JSON pointer to dot-separated RHF path
    const fieldPath = pointer.replace(/\//g, ".") as FieldPath<T>;
    setError(fieldPath, { type: "server", message });
  }
}
