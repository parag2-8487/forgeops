import { describe, expect, it, vi } from "vitest";
import { ApiProblemError } from "@/lib/api";
import { applyFieldErrors } from "@/lib/form-errors";

describe("applyFieldErrors - JSON pointer mapping", () => {
  it("maps JSON pointer field errors to setError calls", () => {
    const error = new ApiProblemError({
      type: "urn:test:validation",
      title: "Validation Error",
      status: 422,
      errors: [
        { pointer: "#/name", detail: "Name is required" },
        { pointer: "#/email", detail: "Invalid email" },
      ],
    });

    const setError = vi.fn();
    applyFieldErrors(error, setError);

    expect(setError).toHaveBeenCalledTimes(2);
    expect(setError).toHaveBeenCalledWith("name", { type: "server", message: "Name is required" });
    expect(setError).toHaveBeenCalledWith("email", { type: "server", message: "Invalid email" });
  });

  it("converts nested JSON pointer paths (slash-separated) to dot-separated paths", () => {
    const error = new ApiProblemError({
      type: "urn:test:validation",
      title: "Validation Error",
      status: 422,
      errors: [
        { pointer: "#/address/city", detail: "City is required" },
        { pointer: "#/contacts/0/phone", detail: "Invalid phone" },
      ],
    });

    const setError = vi.fn();
    applyFieldErrors(error, setError);

    expect(setError).toHaveBeenCalledWith("address.city", {
      type: "server",
      message: "City is required",
    });
    expect(setError).toHaveBeenCalledWith("contacts.0.phone", {
      type: "server",
      message: "Invalid phone",
    });
  });

  it("handles empty errors array gracefully", () => {
    const error = new ApiProblemError({
      type: "urn:test:validation",
      title: "Validation Error",
      status: 422,
      errors: [],
    });

    const setError = vi.fn();
    applyFieldErrors(error, setError);
    expect(setError).not.toHaveBeenCalled();
  });

  it("handles missing errors (undefined) gracefully", () => {
    const error = new ApiProblemError({
      type: "urn:test",
      title: "Error",
      status: 500,
    });

    const setError = vi.fn();
    applyFieldErrors(error, setError);
    expect(setError).not.toHaveBeenCalled();
  });
});
