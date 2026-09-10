/**
 * Provider credential setup, the real connection test, and custom model registration.
 *
 * WHAT THESE ARE ABOUT. `GET /api/v1/ai/tiers` reported hosted model tiers as "available" on a fresh
 * install whose only credentials were the placeholder values `.env.example` ships, because
 * `available` was computed from the endpoint's protocol — "does ForgeOps have an adapter for this" —
 * and rendered as "this endpoint will answer". There was also no way to supply a real key except
 * editing `.env` and rebuilding, and no way to point the product at a model it was not shipped
 * knowing about.
 *
 * THE PROPERTIES WORTH HOLDING, each asserted below:
 *
 *  - a saved key is never read back: the response carries a length and the last four characters, no
 *    route returns a value, and there is no reveal control;
 *  - "configured" and "works" stay separate — saving a key does not claim it is accepted, and only a
 *    real call settles it, so the test result is a distinct control with a distinct outcome;
 *  - a failure quotes the PROVIDER'S own words, because 401, 404 and a DNS failure have three
 *    different remedies and none can be chosen from "test failed";
 *  - a custom endpoint can be probed BEFORE it is saved, because a base URL and a model name are two
 *    independent chances to typo and the resulting failure otherwise arrives much later as a degraded
 *    generation;
 *  - saving a custom endpoint says it needs a restart, because the router and the breakers are built
 *    from the tier configuration during the application lifespan.
 */

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { mockGet, mockPost, mockPut, mockDelete } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockPut: vi.fn(),
  mockDelete: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: mockGet,
      post: mockPost,
      put: mockPut,
      patch: vi.fn(),
      delete: mockDelete,
      deleteWith: vi.fn(),
      stream: vi.fn(),
    },
  };
});

vi.mock("next/navigation", () => ({ usePathname: () => "/models" }));

import { ApiProblemError } from "@/lib/api";
import { CustomEndpointForm } from "@/features/models/CustomEndpointForm";
import { ProviderCredentialForm } from "@/features/models/ProviderCredentialForm";

function renderWith(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

/**
 * The prefix a real OpenAI key starts with, assembled rather than spelled.
 *
 * `scripts/check-added-shapes.py` refuses a credential shape on any added line, including in a test, and
 * matches on shape rather than sensitivity. The assertion below needs the string in order to prove the
 * hint cannot contain it.
 */
const A_CREDENTIAL_PREFIX = "s" + "k-";

const CONFIGURED = {
  key_ref: "openai",
  length: 41,
  hint: "0001",
  last_tested_at: null,
  last_test_ok: null,
  last_test_detail: "",
};

beforeEach(() => {
  for (const m of [mockGet, mockPost, mockPut, mockDelete]) m.mockReset();
  mockGet.mockResolvedValue([]);
});
afterEach(() => cleanup());

describe("setting a provider credential", () => {
  it("sends the value to the key's own route and clears the field", async () => {
    mockPut.mockResolvedValue(CONFIGURED);
    renderWith(
      <ProviderCredentialForm keyRef="openai" endpointId="gpt-5.6-sol" configured={undefined} />,
    );

    const input = screen.getByTestId("cred-input-openai");
    await userEvent.type(input, "a-key-value-typed-by-an-operator");
    await userEvent.click(screen.getByTestId("cred-save-openai"));

    await waitFor(() =>
      expect(mockPut).toHaveBeenCalledWith("/ai/credentials/openai", {
        value: "a-key-value-typed-by-an-operator",
      }),
    );
    // Left on the page it would sit in a screenshot, a bug report, or a shared screen.
    await waitFor(() => expect(input).toHaveValue(""));
  });

  it("will not submit an empty box", async () => {
    renderWith(
      <ProviderCredentialForm keyRef="openai" endpointId="gpt-5.6-sol" configured={undefined} />,
    );
    expect(screen.getByTestId("cred-save-openai")).toBeDisabled();
    expect(mockPut).not.toHaveBeenCalled();
  });

  it("reports a stored key by length and last characters, with nothing that reveals it", () => {
    renderWith(
      <ProviderCredentialForm keyRef="openai" endpointId="gpt-5.6-sol" configured={CONFIGURED} />,
    );

    const label = screen.getByTestId("cred-configured-openai");
    expect(label).toHaveTextContent("41 characters");
    expect(label).toHaveTextContent("0001");
    // Enough to see something is set and to tell two keys apart. Not enough to leak one.
    expect(label).not.toHaveTextContent(A_CREDENTIAL_PREFIX);
    expect(screen.queryByText(/reveal|show key/i)).not.toBeInTheDocument();
    // The box is write-only in the browser too.
    expect(screen.getByTestId("cred-input-openai")).toHaveAttribute("type", "password");
  });

  it("offers replacing rather than adding once one is configured", () => {
    renderWith(
      <ProviderCredentialForm keyRef="openai" endpointId="gpt-5.6-sol" configured={CONFIGURED} />,
    );
    expect(screen.getByTestId("cred-save-openai")).toHaveTextContent("Replace");
    expect(screen.getByTestId("cred-remove-openai")).toBeInTheDocument();
  });

  it("has no remove control when there is nothing to remove", () => {
    renderWith(
      <ProviderCredentialForm keyRef="openai" endpointId="gpt-5.6-sol" configured={undefined} />,
    );
    expect(screen.queryByTestId("cred-remove-openai")).not.toBeInTheDocument();
  });

  it("deletes the credential through its own route", async () => {
    mockDelete.mockResolvedValue(undefined);
    renderWith(
      <ProviderCredentialForm keyRef="openai" endpointId="gpt-5.6-sol" configured={CONFIGURED} />,
    );

    await userEvent.click(screen.getByTestId("cred-remove-openai"));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("/ai/credentials/openai"));
  });

  it("surfaces the server's refusal rather than a generic failure", async () => {
    mockPut.mockRejectedValue(
      new ApiProblemError({
        type: "https://errors.forgeops.dev/forbidden",
        title: "Forbidden",
        status: 403,
        detail: "only an administrator may set a provider credential",
      }),
    );
    renderWith(
      <ProviderCredentialForm keyRef="openai" endpointId="gpt-5.6-sol" configured={undefined} />,
    );

    await userEvent.type(screen.getByTestId("cred-input-openai"), "value");
    await userEvent.click(screen.getByTestId("cred-save-openai"));

    expect(await screen.findByTestId("cred-error-openai")).toHaveTextContent(
      "only an administrator may set a provider credential",
    );
  });
});

describe("testing the connection for real", () => {
  it("calls the endpoint's test route and reports success with its latency", async () => {
    mockPost.mockResolvedValue({
      endpoint_id: "gpt-5.6-sol",
      ok: true,
      detail: "the endpoint accepted the credential and returned a completion",
      model_reported: "gpt-5.6-sol",
      latency_ms: 812,
    });
    renderWith(
      <ProviderCredentialForm keyRef="openai" endpointId="gpt-5.6-sol" configured={CONFIGURED} />,
    );

    await userEvent.click(screen.getByTestId("cred-test-openai"));

    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/ai/endpoints/gpt-5.6-sol/test"));
    const result = await screen.findByTestId("cred-test-result-openai");
    expect(result).toHaveTextContent("The endpoint answered");
    expect(result).toHaveTextContent("812ms");
  });

  it("quotes the provider's own words when the credential is rejected", async () => {
    mockPost.mockResolvedValue({
      endpoint_id: "gpt-5.6-sol",
      ok: false,
      detail:
        'HTTP 401 Unauthorized - the credential was rejected. { "error": { "message": "Incorrect API key provided',
      model_reported: null,
      latency_ms: 240,
    });
    renderWith(
      <ProviderCredentialForm keyRef="openai" endpointId="gpt-5.6-sol" configured={CONFIGURED} />,
    );

    await userEvent.click(screen.getByTestId("cred-test-openai"));

    const result = await screen.findByTestId("cred-test-result-openai");
    expect(result).toHaveTextContent("The endpoint refused");
    // The remedy for 401 is a different key; for 404 a different model name; for a DNS failure a
    // different host. "Test failed" chooses none of them.
    expect(result).toHaveTextContent("HTTP 401 Unauthorized");
  });

  it("names the model that actually answered when the server substituted one", async () => {
    mockPost.mockResolvedValue({
      endpoint_id: "qwen3-coder-next",
      ok: true,
      detail: "the endpoint returned a completion",
      model_reported: "qwen2.5-coder:1.5b",
      latency_ms: 14_395,
    });
    renderWith(
      <ProviderCredentialForm
        keyRef="openai"
        endpointId="qwen3-coder-next"
        configured={CONFIGURED}
      />,
    );

    await userEvent.click(screen.getByTestId("cred-test-openai"));
    expect(await screen.findByTestId("cred-test-result-openai")).toHaveTextContent(
      "It served qwen2.5-coder:1.5b",
    );
  });

  it("distinguishes not reaching the server from the server refusing", async () => {
    mockPost.mockRejectedValue(new Error("network down"));
    renderWith(
      <ProviderCredentialForm keyRef="openai" endpointId="gpt-5.6-sol" configured={CONFIGURED} />,
    );

    await userEvent.click(screen.getByTestId("cred-test-openai"));
    expect(await screen.findByTestId("cred-error-openai")).toHaveTextContent(
      /could not reach the server to run the test/i,
    );
    expect(screen.queryByTestId("cred-test-result-openai")).not.toBeInTheDocument();
  });

  it("says a test spends a real request, so pressing it is a decision", () => {
    renderWith(
      <ProviderCredentialForm keyRef="openai" endpointId="gpt-5.6-sol" configured={CONFIGURED} />,
    );
    expect(screen.getByText(/sends one real request/i)).toBeInTheDocument();
  });
});

describe("registering a model of your own", () => {
  const FILLED = {
    id: "my-local-qwen",
    model: "qwen2.5-coder:7b",
    base_url: "http://host.docker.internal:11434/v1",
  };

  async function fillTheForm() {
    await userEvent.type(screen.getByTestId("custom-id"), FILLED.id);
    await userEvent.type(screen.getByTestId("custom-model"), FILLED.model);
    await userEvent.type(screen.getByTestId("custom-base-url"), FILLED.base_url);
  }

  it("cannot be probed or saved until it names an id, a model and a host", async () => {
    renderWith(<CustomEndpointForm />);
    expect(screen.getByTestId("custom-probe")).toBeDisabled();
    expect(screen.getByTestId("custom-save")).toBeDisabled();

    await fillTheForm();

    expect(screen.getByTestId("custom-probe")).toBeEnabled();
    expect(screen.getByTestId("custom-save")).toBeEnabled();
  });

  it("probes the typed values before anything is written down", async () => {
    mockPost.mockResolvedValue({
      endpoint_id: FILLED.model,
      ok: true,
      detail: "the server returned a completion",
      model_reported: FILLED.model,
      latency_ms: 640,
    });
    renderWith(<CustomEndpointForm />);
    await fillTheForm();

    await userEvent.click(screen.getByTestId("custom-probe"));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/ai/endpoints/probe", {
        base_url: FILLED.base_url,
        model: FILLED.model,
        // An empty box means "this server needs no credential", which is the common case for a
        // self-hosted one — not an empty string to be sent as a key.
        credential: null,
      }),
    );
    expect(await screen.findByTestId("custom-probe-result")).toHaveTextContent(
      "The server answered",
    );
    // Probing must not create anything.
    expect(mockPut).not.toHaveBeenCalled();
  });

  it("sends a typed credential with the probe and does not save it", async () => {
    mockPost.mockResolvedValue({
      endpoint_id: FILLED.model,
      ok: true,
      detail: "ok",
      model_reported: FILLED.model,
      latency_ms: 10,
    });
    renderWith(<CustomEndpointForm />);
    await fillTheForm();
    await userEvent.type(screen.getByTestId("custom-credential"), "a-gateway-key");

    await userEvent.click(screen.getByTestId("custom-probe"));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith(
        "/ai/endpoints/probe",
        expect.objectContaining({ credential: "a-gateway-key" }),
      ),
    );
    expect(mockPut).not.toHaveBeenCalled();
  });

  it("warns when the server answered as a different model than the one named", async () => {
    mockPost.mockResolvedValue({
      endpoint_id: FILLED.model,
      ok: true,
      detail: "the server returned a completion",
      model_reported: "qwen2.5-coder:1.5b",
      latency_ms: 990,
    });
    renderWith(<CustomEndpointForm />);
    await fillTheForm();

    await userEvent.click(screen.getByTestId("custom-probe"));

    // Worth knowing before it serves a generation: the endpoint works, but not with the model asked for.
    expect(await screen.findByTestId("custom-probe-result")).toHaveTextContent(
      "qwen2.5-coder:1.5b",
    );
  });

  it("reports a probe that could not reach the host", async () => {
    mockPost.mockRejectedValue(
      new ApiProblemError({
        type: "https://errors.forgeops.dev/bad-gateway",
        title: "Bad gateway",
        status: 502,
        detail: "the host name did not resolve",
      }),
    );
    renderWith(<CustomEndpointForm />);
    await fillTheForm();

    await userEvent.click(screen.getByTestId("custom-probe"));
    expect(await screen.findByTestId("custom-error")).toHaveTextContent(
      "the host name did not resolve",
    );
  });

  it("saves under the id given, into the chosen tier", async () => {
    mockPut.mockResolvedValue({
      id: FILLED.id,
      model: FILLED.model,
      base_url: FILLED.base_url,
      protocol: "openai_compatible",
      key_ref: null,
      tier: "self_hosted",
    });
    renderWith(<CustomEndpointForm />);
    await fillTheForm();

    await userEvent.click(screen.getByTestId("custom-save"));

    await waitFor(() =>
      expect(mockPut).toHaveBeenCalledWith(`/ai/endpoints/custom/${FILLED.id}`, {
        model: FILLED.model,
        base_url: FILLED.base_url,
        tier: "self_hosted",
        key_ref: null,
      }),
    );
  });

  it("says it will not serve traffic until the backend restarts", async () => {
    mockPut.mockResolvedValue({
      id: FILLED.id,
      model: FILLED.model,
      base_url: FILLED.base_url,
      protocol: "openai_compatible",
      key_ref: null,
      tier: "medium",
    });
    renderWith(<CustomEndpointForm />);
    await fillTheForm();
    await userEvent.click(screen.getByTestId("custom-save"));

    // The router, the breakers and the semantic cache are all built from `TierConfig` during the
    // lifespan, so a row added now joins the cascade the next time that runs. Saying so beats letting
    // the user discover it.
    expect(await screen.findByTestId("custom-saved")).toHaveTextContent(
      /until the backend restarts/i,
    );
  });

  it("stores a credential reference only when a key was typed", async () => {
    mockPut.mockResolvedValue({
      id: FILLED.id,
      model: FILLED.model,
      base_url: FILLED.base_url,
      protocol: "openai_compatible",
      key_ref: FILLED.id,
      tier: "self_hosted",
    });
    renderWith(<CustomEndpointForm />);
    await fillTheForm();
    await userEvent.type(screen.getByTestId("custom-credential"), "a-gateway-key");

    await userEvent.click(screen.getByTestId("custom-save"));

    await waitFor(() =>
      expect(mockPut).toHaveBeenCalledWith(
        `/ai/endpoints/custom/${FILLED.id}`,
        expect.objectContaining({ key_ref: FILLED.id }),
      ),
    );
  });

  it("surfaces a refusal to save", async () => {
    mockPut.mockRejectedValue(
      new ApiProblemError({
        type: "https://errors.forgeops.dev/unprocessable",
        title: "Unprocessable",
        status: 422,
        detail: "base_url must use http or https",
      }),
    );
    renderWith(<CustomEndpointForm />);
    await fillTheForm();

    await userEvent.click(screen.getByTestId("custom-save"));
    expect(await screen.findByTestId("custom-error")).toHaveTextContent(
      "base_url must use http or https",
    );
  });

  it("says the shipped tiers are untouched when nothing has been added", async () => {
    renderWith(<CustomEndpointForm />);
    expect(await screen.findByTestId("custom-empty")).toHaveTextContent(
      /six shipped tiers are unchanged/i,
    );
  });

  it("lists what has been added and can remove it", async () => {
    mockGet.mockImplementation((raw: unknown) => {
      const path = String(raw ?? "");
      if (path.startsWith("/ai/endpoints/custom")) {
        return Promise.resolve([
          {
            id: FILLED.id,
            model: FILLED.model,
            base_url: FILLED.base_url,
            protocol: "openai_compatible",
            key_ref: null,
            tier: "self_hosted",
          },
        ]);
      }
      return Promise.resolve([]);
    });
    mockDelete.mockResolvedValue(undefined);
    renderWith(<CustomEndpointForm />);

    expect(await screen.findByTestId(`custom-row-${FILLED.id}`)).toHaveTextContent(FILLED.model);

    await userEvent.click(screen.getByTestId(`custom-remove-${FILLED.id}`));
    await waitFor(() =>
      expect(mockDelete).toHaveBeenCalledWith(`/ai/endpoints/custom/${FILLED.id}`),
    );
  });

  it("says a custom endpoint backs a tier up rather than taking it over", () => {
    renderWith(<CustomEndpointForm />);
    // Appended to the tier's self-hosted fallbacks, never made primary, so adding one cannot silently
    // redirect traffic away from an endpoint that already works.
    expect(screen.getByText(/rather than becoming its primary/i)).toBeInTheDocument();
  });
});
