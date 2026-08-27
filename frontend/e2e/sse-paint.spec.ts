// SPDX-License-Identifier: FSL-1.1-ALv2

/**
 * Criterion 13's BROWSER half: the model's tokens are painted as they arrive.
 *
 * WHY THIS IS A SEPARATE ASSERTION FROM STEP 7. The journey's steps 6 and 7 post to
 * `POST /api/v1/generation/runs` through `page.request` and assert on the response body. That proves
 * the TRANSPORT — six event names, the documented order, one terminal event — and it proved it while
 * the browser painted nothing at all. `generation/service.py` emits `token` events carrying
 * `{run_id, text, attempt}` and no `path`, because mid-stream the output is one undifferentiated blob
 * and which file a token belongs to is unknown until `parse_artifacts` runs. `GeneratorWizard.tsx`
 * buffered only `if (payload.path)`, so every token was dropped and `stream-output` never appeared.
 * Every server-side well-formedness test passed throughout. That is the gap this file closes: the
 * criterion says the tokens are PAINTED, and only the DOM can answer that.
 *
 * WHY A MutationObserver RATHER THAN POLLING, and rather than watching the network. Three approaches
 * were considered and the first was tried and abandoned:
 *
 *   - POLLING ON A TIMER RACES THE STREAM. A previous attempt sampled `stream-output` every 500 ms
 *     and could not reliably catch three increments on a ~1100-character artifact: it observed real
 *     growth (984 then 1102 characters) but the run finished between samples. Making the interval
 *     shorter narrows the window without closing it, because the sampler and the stream are
 *     independent clocks. That is a design flaw, not a tuning problem.
 *   - INTERCEPTING THE SSE FRAMES would prove the bytes arrived, which is what step 7 already proves
 *     and exactly what was true while the bug was live. It cannot distinguish "delivered" from
 *     "painted", so it would not have caught the defect this test exists for.
 *   - A MutationObserver IS EVENT-DRIVEN, so it cannot miss an update: the browser calls it for every
 *     batch of DOM changes. There is no interval to tune and no race to lose.
 *
 * It observes `document.body` with `subtree` and `characterData` rather than the `<pre>` itself,
 * because the `<pre>` DOES NOT EXIST YET when generation starts — `GeneratorWizard` renders it only
 * once `liveOutput !== ""`. Observing the element would mean waiting for it, which is the polling
 * problem again. Observing the body catches its creation and every subsequent text change.
 */

import { expect, test } from "@playwright/test";

import { gotoAsOperator, OPERATOR } from "./helpers/auth";
import { sqlScalar } from "./helpers/stack";

/**
 * A prompt chosen to be SEMANTICALLY DISTINCT from the journey's, and no larger than it needs to be.
 *
 * DISTINCT, because the six-tier router's L1 cache is a digest and its L2 is a cosine similarity at
 * >= 0.95. A prompt that merely appends a timestamp defeats L1 and not L2 — the run is then served
 * from cache in microseconds and paints in a single update. Observed exactly that: `Lengths: 1071`,
 * one sample, from a cache hit. A different language and a different tool put the embedding far
 * enough away that the tier which answers is a model rather than a cache.
 *
 * SMALL, because a MutationObserver does not need volume. An earlier version of this prompt also
 * asked for a Kubernetes CronJob and a comment explaining every stage; the run was still emitting
 * tokens after twenty minutes and never reached `complete`. That was the right instinct for a
 * SAMPLING test — more deltas make a timer less likely to miss one — and it is unnecessary here,
 * because the observer is called for every DOM change and cannot miss any. Asking for less output
 * makes the test faster without making it weaker.
 */
const PROMPT =
  "Write a multi-stage Dockerfile for a Rust service built with cargo, using a distroless runtime stage.";

test.describe("Criterion 13: the model's tokens are painted as they arrive", () => {
  test("the stream output grows across at least three distinct renders", async ({ page }) => {
    test.skip(
      OPERATOR.password === "",
      "E2E_OIDC_PASSWORD is unset, so there is no operator to sign in as",
    );
    // A real model call on CPU, and the wizard runs the same six-tier router the journey does. The
    // budget matches step 6's for the same reason: the deterministic gate may ask for more than one
    // attempt, and capping attempts to fit a smaller budget would change the product to suit a test.
    // 40 minutes. The first run with this prompt reached `accepted` / `provider` in the database but
    // only AFTER the 19-minute wait had expired — the model answered and the gate accepted, and the
    // browser had already given up. Raising the bound is not weakening anything: every assertion below
    // is unchanged, and the alternative is a test that fails for lack of patience rather than for a
    // defect. On an idle model server the run is far quicker; the budget covers a contended one.
    test.setTimeout(2_400_000);

    // THE JOURNEY'S PROJECT, not a fresh one.
    //
    // Three earlier attempts created their own project and all three ended `validation -> error`
    // instead of `complete`, while `generation_runs` recorded the run as `accepted`. Generation and the
    // deterministic gate had both SUCCEEDED; what failed was the step after them. A change set is
    // admitted by the governance chokepoint only when the submitting device's pinned policy-bundle
    // digest matches the project's active one, and a project created by this test has neither a
    // published bundle nor a paired device -- so there was nothing to admit it. `change_sets` held no
    // row for any of those runs, which is what identified the cause rather than guesswork about the
    // prompt or the model.
    //
    // The journey's steps 2 and 3 establish exactly those prerequisites, so this reuses their project
    // instead of reproducing a pairing handshake that step 3 already covers.
    const found = sqlScalar(
      "SELECT id FROM projects WHERE name = 'e2e-fixture' ORDER BY created_at DESC LIMIT 1",
    );
    expect(
      found,
      "no `e2e-fixture` project: run the journey first -- it pairs the device and publishes the " +
        "bundle that the change-set submission at the end of a run requires",
    ).not.toBeNull();
    const projectId = found as string;

    await gotoAsOperator(page, "/generation");
    // `ProjectPicker` renders a <select>, not a text field — the first attempt used `fill` and got
    // "Element is not an <input>, <textarea> or [contenteditable] element".
    const picker = page.locator("#generation-project");
    await expect(picker.locator(`option[value="${projectId}"]`)).toBeAttached({ timeout: 30_000 });
    await picker.selectOption(projectId);
    await page.locator("#generation-prompt").fill(PROMPT);

    // INSTALL THE OBSERVER BEFORE THE CLICK, so nothing can be missed between starting the run and
    // the first token. It records a length only when the length CHANGES, so a re-render that paints
    // the same text does not manufacture a fake increment.
    await page.evaluate(() => {
      const w = window as unknown as { __paint?: number[]; __paintObserver?: MutationObserver };
      w.__paint = [];
      const read = () => {
        const pre = document.querySelector('[data-testid="stream-output"]');
        if (!pre) return;
        const len = (pre.textContent ?? "").length;
        const seen = w.__paint!;
        if (len !== seen[seen.length - 1]) seen.push(len);
      };
      const observer = new MutationObserver(read);
      // `characterData` catches text growing inside the existing <pre>; `childList` with `subtree`
      // catches the <pre> being created in the first place. Both are needed: React may replace the
      // text node rather than mutate it.
      observer.observe(document.body, {
        childList: true,
        subtree: true,
        characterData: true,
      });
      w.__paintObserver = observer;
      read();
    });

    await page.getByRole("button", { name: /generate artifacts/i }).click();

    // The run is finished when the wizard says so. `complete` is the stream's terminal event and the
    // wizard records the names it saw in `event-log`, so this waits on the application's own account
    // of the stream rather than on a duration.
    //
    // ON FAILURE, REPORT WHAT WAS PAINTED. A bare timeout here says only "complete never arrived",
    // which is the least useful half of what this test knows — an earlier run timed out having painted
    // dozens of tokens, and that distinction (the stream was live but slow, versus nothing rendered at
    // all) is the whole diagnostic value. So the observer is read before the error is re-raised.
    try {
      await expect(page.getByTestId("event-log")).toContainText("complete", {
        timeout: 2_300_000,
      });
    } catch (cause) {
      const partial = await page.evaluate(() => {
        const w = window as unknown as { __paint?: number[] };
        return w.__paint ?? [];
      });
      const log =
        (await page
          .getByTestId("event-log")
          .textContent()
          .catch(() => "")) ?? "";
      throw new Error(
        `the stream never reached 'complete'.\n` +
          `  painted lengths so far: ${JSON.stringify(partial)}\n` +
          `  events seen: ${log}\n` +
          `  original: ${cause instanceof Error ? cause.message.split("\n")[0] : String(cause)}`,
      );
    }

    const lengths = await page.evaluate(() => {
      const w = window as unknown as { __paint?: number[]; __paintObserver?: MutationObserver };
      w.__paintObserver?.disconnect();
      return w.__paint ?? [];
    });

    // Drop a leading zero: the observer's first call can fire before any token has arrived, and a
    // zero-length reading is the absence of output rather than a paint of it.
    const painted = lengths.filter((n) => n > 0);

    // THE ASSERTION THE CRITERION ACTUALLY MAKES. Three or more distinct lengths means the text was
    // on screen in at least three different states, which a buffered response cannot produce: it
    // paints once, from nothing to everything.
    expect(
      painted.length,
      `stream-output was painted ${painted.length} time(s); lengths: ${JSON.stringify(lengths)}`,
    ).toBeGreaterThanOrEqual(3);

    // STRICTLY INCREASING. Tokens append, so length only grows. A decrease would mean the wizard
    // replaced the buffer instead of extending it — which is a different bug from dropping tokens and
    // would otherwise look like success.
    for (let i = 1; i < painted.length; i++) {
      expect(
        painted[i],
        `paint ${i} shrank from ${painted[i - 1]} to ${painted[i]}: ${JSON.stringify(painted)}`,
      ).toBeGreaterThan(painted[i - 1]);
    }

    // The final paint must match what is actually on screen, so the observer is measuring the same
    // element the operator reads.
    const finalText = await page.getByTestId("stream-output").textContent();
    expect((finalText ?? "").length).toBe(painted[painted.length - 1]);

    // AND THE EVENT VOCABULARY THE WIZARD OBSERVED, from the browser's side this time. Step 7 asserts
    // this on the raw bytes; here it is what the component actually dispatched on.
    const log = (await page.getByTestId("event-log").textContent()) ?? "";
    const names = log
      .replace(/^[^:]*:/, "")
      .split("\u2192")
      .map((n) => n.trim())
      .filter((n) => n !== "");

    expect(names[0], `the first event was not status: ${log}`).toBe("status");
    expect(names.filter((n) => n === "token").length).toBeGreaterThanOrEqual(3);
    expect(names, `no progress event: ${log}`).toContain("progress");
    expect(names, `an error event arrived: ${log}`).not.toContain("error");
    expect(names[names.length - 1], `the stream did not end on complete: ${log}`).toBe("complete");

    // The observed lengths ARE the evidence for criterion 13, so they go to the run log.
    console.log(`criterion 13 — painted lengths: ${JSON.stringify(painted)}`);
  });
});
