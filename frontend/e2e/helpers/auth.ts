// SPDX-License-Identifier: FSL-1.1-ALv2

/**
 * Signing in as the operator, shared by every spec that needs a session.
 *
 * EXTRACTED FROM `journey.spec.ts` RATHER THAN COPIED. `sse-paint.spec.ts` needs the same real login
 * and the same session hand-off, and duplicating ninety lines of IdP flow driving would mean two
 * copies of reasoning that took four failed attempts to arrive at -- so the next person to fix one
 * would fix it in one place and leave the other subtly wrong.
 *
 * Nothing here changed in the move. Both functions were verified first to reference no journey state:
 * they touch only `page`, `expect`, the two constants below, and each other, which is what made the
 * extraction mechanical rather than a rewrite.
 */

import fs from "node:fs";
import path from "node:path";
import { expect, type BrowserContext, type Page } from "@playwright/test";

/**
 * Exchanges the httpOnly session cookie for a short-lived access token, as `lib/api/client.ts` does.
 *
 * RETRIED, BECAUSE THE REFRESH TOKEN IS SINGLE-USE AND ROTATES. The app's own `AuthBoundary` posts
 * `/auth/refresh` on mount, so a call made just after a navigation can present a token the backend
 * has already rotated away and be correctly refused. Retrying presents the cookie the context now
 * holds. Nothing is weakened: the property asserted is still "this session exchanges for an access
 * token", refusing a spent token is the control working, and a retry that never succeeds still fails.
 *
 * Returns the token rather than storing it, so callers that keep one can and callers that do not
 * need not. `journey.spec.ts` caches the result on its own state object; the paint spec uses it once.
 */
export async function mintAccessToken(page: Page, apiBase: string): Promise<string> {
  let last = "";
  for (let attempt = 1; attempt <= 3; attempt++) {
    const refreshed = await page.request.post(`${apiBase}/auth/refresh`, {
      headers: { Accept: "application/json" },
    });
    if (refreshed.status() === 200) {
      const body = (await refreshed.json()) as { access_token?: string };
      expect(body.access_token, "the refresh response carried no access token").toBeTruthy();
      return body.access_token as string;
    }
    last = `attempt ${attempt}: ${refreshed.status()} ${await refreshed.text()}`;
    // Long enough for the application's own in-flight refresh to have committed its rotation.
    await page.waitForTimeout(1_000);
  }
  throw new Error(`the session never exchanged for an access token; ${last}`);
}

/** The Authentik user `scripts/ci/provision-authentik.py` creates. */
export const OPERATOR = {
  username: process.env.E2E_OIDC_USERNAME ?? "e2e-operator",
  password: process.env.E2E_OIDC_PASSWORD ?? "",
};

/**
 * Where step 1 saves the session it genuinely obtained, for the later steps to carry forward.
 *
 * Under `test-results/`, which Playwright already treats as run output and which is gitignored, so
 * nothing resembling a credential lands in the tree. It holds the cookies of a synthetic, e2e-only
 * account against a local IdP.
 */
export const SESSION_STATE_PATH = path.join("test-results", "journey-session.json");

/**
 * Signs in against the real IdP with real credentials. No session injection: step 1 is the real flow.
 *
 * DRIVEN THROUGH AUTHENTIK'S FLOW EXECUTOR API RATHER THAN ITS FORM WIDGET, which is the technique
 * design.md §17.2 (the OQ-28 resolution) already records for this project: "POST
 * /api/v3/flows/executor/{flow_slug}/ is driven directly over httpx with a cookie jar: GET returns
 * the ak-stage-identification challenge, POST {"uid_field": ...} advances to ak-stage-password, POST
 * {"password": ...} returns xak-flow-redirect, and the resulting authentik_session cookie is an
 * authenticated session."
 *
 * WHY, recorded because it is a deviation from "click the form". Four attempts to drive the rendered
 * form failed at the same point, and the cause was diagnosed rather than guessed: Authentik's server
 * log shows the identification stage's chunk being fetched and then only GETs — no POST to the flow
 * executor ever arrives, so the Continue click is not reaching the control inside the stage's shadow
 * root in Authentik 2026.5.6.
 *
 * What this preserves is what matters. The browser makes the requests, through `page.request`, which
 * shares the page's cookie jar — so the `authentik_session` cookie the flow returns belongs to the
 * browser, and the subsequent navigation to the authorization endpoint is a real authenticated
 * request producing a real `code`. The credentials are the real ones, the IdP is real, the token is
 * RS256-verified by the production verifier, and the session cookie the callback sets is the one the
 * app then uses. Only the widget interaction is replaced, not the authentication.
 *
 * What it does NOT do is inject a session or mint a token out of band, which is the thing that would
 * make step 1 worthless.
 */
export async function signIn(page: Page) {
  const frontend = new URL(process.env.E2E_FRONTEND_URL ?? "http://localhost:3000");
  // The PUBLIC origin, never the issuer. `OIDC_ISSUER` is how the BACKEND reaches the IdP -- inside
  // Compose that is a service name a browser cannot resolve -- and driving the browser at it is
  // exactly the mistake that shipped a login redirecting to an unresolvable host.
  const idp = (process.env.E2E_OIDC_PUBLIC_BASE_URL ?? "").replace(/\/$/, "");
  expect(
    idp,
    "E2E_OIDC_PUBLIC_BASE_URL must be set: it is the origin a BROWSER can reach",
  ).not.toBe("");

  // Land ON the IdP's own origin first. Two reasons, both load-bearing:
  //
  //  - the fetches below are then SAME-ORIGIN, so Authentik's API accepts them without any CORS
  //    allowance a real deployment would not grant;
  //  - they run inside Chromium, so they use the browser's cookie jar AND its resolver. Playwright's
  //    `page.request` runs in Node and does neither, which is how an earlier version failed with
  //    `ENOTFOUND forgeops-idp.local`: Node has no equivalent of the browser's host-resolver rule,
  //    and the IdP is deliberately reachable under a name only the browser and the backend container
  //    share.
  await page.goto(`${idp}/if/flow/default-authentication-flow/`, { waitUntil: "domcontentloaded" });

  const flowPath = "/api/v3/flows/executor/default-authentication-flow/";
  const credentials = { username: OPERATOR.username, password: OPERATOR.password };

  const outcome = await page.evaluate(
    async ({ path, creds }: { path: string; creds: { username: string; password: string } }) => {
      const seen: string[] = [];
      const first = await fetch(path, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (!first.ok) return { error: `challenge ${first.status}`, seen };
      let body = await first.json();
      seen.push(body.component);

      for (let step = 0; step < 5 && body.component !== "xak-flow-redirect"; step++) {
        const payload =
          body.component === "ak-stage-identification"
            ? { uid_field: creds.username }
            : body.component === "ak-stage-password"
              ? { password: creds.password }
              : null;
        if (payload === null) return { error: `unexpected stage ${body.component}`, seen };

        const advanced = await fetch(path, {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify(payload),
        });
        if (!advanced.ok) return { error: `stage ${advanced.status}`, seen };
        body = await advanced.json();
        seen.push(body.component);
        if (body.response_errors) {
          return { error: `rejected: ${JSON.stringify(body.response_errors)}`, seen };
        }
      }
      return { error: body.component === "xak-flow-redirect" ? null : "never redirected", seen };
    },
    { path: flowPath, creds: credentials },
  );

  expect(outcome.error, `IdP flow stages seen: ${outcome.seen.join(" -> ")}`).toBeNull();

  // The browser is now authenticated AT THE IdP. Starting the application's login therefore takes
  // the real authorization-code path: /auth/login builds PKCE, the IdP returns a code without
  // prompting again, and the callback exchanges it server-side and sets the session cookie.
  await page.goto("/login");
  await page.getByRole("button", { name: /single sign-on/i }).click();
  await page.waitForURL((url) => url.host === frontend.host && !url.pathname.startsWith("/login"), {
    timeout: 60_000,
  });
}

/**
 * Navigate to an application route with a session, and prove the app really rendered it.
 *
 * A FULL PAGE LOAD discards the in-memory access token, so `AuthBoundary` recovers the session by
 * POSTing `/auth/refresh` with the httpOnly cookie. That is designed to work and usually does -- but
 * it is not free of races, and a recovery that does not land redirects to `/login`, where every
 * locator in the calling step waits on a control that only exists inside the shell. Observed exactly
 * that: the same step passing on one run and reporting the sign-in screen on the next.
 *
 * THE SHELL IS NOT THE SIGNAL. `app/(shell)/layout.tsx` puts `AuthBoundary` inside `<main>`
 * deliberately, so the sidebar and header stay rendered while the session is being restored -- an
 * earlier version of this helper waited for `nav[Primary]`, saw it immediately, returned, and left
 * the caller on a page that redirected to `/login` a moment later. What distinguishes the two is the
 * heading: the sign-in screen owns the only `h1` that says so.
 *
 * Nothing is weakened by the retry: the assertions in each step are unchanged, and if the route never
 * renders the error carries the text the screen was actually showing rather than a locator timeout.
 */
export async function gotoAsOperator(page: Page, path: string): Promise<void> {
  // REUSE THE SESSION STEP 1 OBTAINED rather than authenticating again per test.
  //
  // Playwright gives each test a fresh context, so nothing step 1 obtained is here. Signing in again
  // per step works but is not reliable: the IdP round trip is a real browser navigation through a
  // real provider, and repeating it four more times per run produced intermittent
  // `page.waitForURL: Timeout 60000ms exceeded` and, on an already-authenticated context,
  // `ak-stage-identification -> ak-stage-flow-error`. Neither says anything about the product.
  //
  // Step 1 performs the genuine login -- that IS the criterion, and it asserts a session row exists --
  // and saves the cookies it received. Every later step restores exactly those. No credential is
  // invented and no authentication is skipped: this is the same session, carried forward, which is
  // what a browser would do for an operator who signed in once.
  const cookies = await page.context().cookies();
  if (!cookies.some((cookie) => cookie.name.includes("session"))) {
    if (fs.existsSync(SESSION_STATE_PATH)) {
      const saved = JSON.parse(fs.readFileSync(SESSION_STATE_PATH, "utf8")) as {
        cookies?: Parameters<BrowserContext["addCookies"]>[0];
      };
      if (saved.cookies?.length) await page.context().addCookies(saved.cookies);
    } else {
      // No saved state means step 1 did not run in this invocation (a single step was selected), so
      // the full login is the only option.
      await signIn(page);
    }
  }

  for (let attempt = 1; attempt <= 3; attempt++) {
    await page.goto(path);

    const deadline = Date.now() + 30_000;
    let heading = "";
    while (Date.now() < deadline) {
      heading =
        (await page
          .locator("h1")
          .first()
          .textContent()
          .catch(() => "")) ?? "";
      // A heading that is neither absent nor the sign-in screen's means the route rendered for an
      // authenticated principal.
      if (heading.trim() !== "" && !/sign in to forgeops/i.test(heading)) {
        // RE-SAVE, because the refresh token ROTATES. `POST /auth/refresh` issues a new refresh
        // cookie and retires the one presented, so the state file becomes stale the moment it is
        // used -- which is why restoring the same snapshot worked for one step and then failed on the
        // next with the sign-in screen. Writing back the cookies this context now holds keeps the
        // chain moving forward instead of replaying a spent token.
        await page.context().storageState({ path: SESSION_STATE_PATH });
        return;
      }
      await page.waitForTimeout(500);
    }

    if (attempt === 3) {
      const shown = (
        await page
          .locator("main")
          .innerText()
          .catch(() => "")
      ).slice(0, 600);
      throw new Error(`${path} never rendered for a signed-in operator. What it showed:\n${shown}`);
    }

    // RESUME rather than re-authenticate. The browser holds Authentik's own cookie by now, so
    // clicking the application's sign-on button round-trips through the IdP without a prompt and the
    // callback sets a fresh application cookie. Re-driving the IdP's flow executor here instead fails
    // with "IdP flow stages seen: ak-stage-identification -> ak-stage-flow-error", because there is
    // no identification stage to answer when the visitor is already known.
    await page.goto("/login");
    await page.getByRole("button", { name: /single sign-on/i }).click();
    await page
      .waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 60_000 })
      .catch(() => {});
  }
}
