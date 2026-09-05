import { describe, expect, it } from "vitest";

import { CONTEXT_LINES, diffLines, hunksOf } from "@/features/approvals/ApprovalCenter";

/**
 * A review surface has to make the change findable.
 *
 * `diffLines` produced a correct row-by-row diff of the WHOLE file and the viewer rendered all of it, so
 * a one-line fix to a forty-line Dockerfile arrived as thirty-nine rows of unchanged context around the
 * one row that mattered. Correct, and not a review.
 */

const FORTY_LINES = Array.from({ length: 40 }, (_, i) => `line ${i + 1}`).join("\n");

describe("hunksOf", () => {
  it("returns no hunks when the two sides are identical", () => {
    // Rendered as "this item changes nothing" rather than as an empty table, which reads as a bug.
    expect(hunksOf(diffLines(FORTY_LINES, FORTY_LINES))).toEqual([]);
  });

  it("keeps only the changed region and its context", () => {
    const edited = FORTY_LINES.replace("line 20", "line 20 CHANGED");
    const hunks = hunksOf(diffLines(FORTY_LINES, edited));

    expect(hunks).toHaveLength(1);
    // One removal, one addition, and CONTEXT_LINES either side.
    expect(hunks[0].rows).toHaveLength(2 + CONTEXT_LINES * 2);
    // The untouched top and bottom of the file are absent, which is the whole point.
    const text = hunks[0].rows.map((r) => r.text).join("\n");
    expect(text).not.toContain("line 1\n");
    expect(text).not.toContain("line 40");
    expect(text).toContain("line 20 CHANGED");
  });

  it("addresses each hunk the way a patch header does", () => {
    const edited = FORTY_LINES.replace("line 20", "line 20 CHANGED");
    const [hunk] = hunksOf(diffLines(FORTY_LINES, edited));
    // Line 20 with three lines of context starts at 17 on both sides, seven lines each way.
    expect(hunk.header).toBe("@@ -17,7 +17,7 @@");
  });

  it("splits distant edits into separate hunks and merges near ones", () => {
    const twoEdits = FORTY_LINES.replace("line 5", "line 5 CHANGED").replace(
      "line 35",
      "line 35 CHANGED",
    );
    expect(hunksOf(diffLines(FORTY_LINES, twoEdits))).toHaveLength(2);

    // Two changes three lines apart share context, so they are ONE hunk: printing them separately would
    // repeat the same context rows and imply the edits are further apart than they are.
    const nearEdits = FORTY_LINES.replace("line 20", "line 20 CHANGED").replace(
      "line 22",
      "line 22 CHANGED",
    );
    expect(hunksOf(diffLines(FORTY_LINES, nearEdits))).toHaveLength(1);
  });

  it("carries real line numbers on both sides", () => {
    const edited = FORTY_LINES.replace("line 20", "line 20 CHANGED");
    const [hunk] = hunksOf(diffLines(FORTY_LINES, edited));
    const removed = hunk.rows.find((r) => r.kind === "removed");
    const added = hunk.rows.find((r) => r.kind === "added");

    expect(removed?.oldLine).toBe(20);
    // A removed line has no position in the new file, and saying 20 would be a claim about a line that
    // is not there.
    expect(removed?.newLine).toBeNull();
    expect(added?.newLine).toBe(20);
    expect(added?.oldLine).toBeNull();
  });

  it("treats a creation as all additions", () => {
    const hunks = hunksOf(diffLines("", "a\nb\nc\n"));
    expect(hunks).toHaveLength(1);
    expect(hunks[0].rows.every((r) => r.kind === "added")).toBe(true);
    expect(hunks[0].rows).toHaveLength(3);
  });

  it("treats a deletion as all removals", () => {
    const hunks = hunksOf(diffLines("a\nb\nc\n", ""));
    expect(hunks).toHaveLength(1);
    expect(hunks[0].rows.every((r) => r.kind === "removed")).toBe(true);
  });

  it("reports an insertion as one added row, not as every following line changed", () => {
    // The failure a naive index-by-index comparison produces, pinned so it cannot come back: inserting
    // one line at the top must not mark the other forty as modified.
    const withHeader = `# new header\n${FORTY_LINES}`;
    const rows = diffLines(FORTY_LINES, withHeader);
    expect(rows.filter((r) => r.kind === "added")).toHaveLength(1);
    expect(rows.filter((r) => r.kind === "removed")).toHaveLength(0);
  });
});
