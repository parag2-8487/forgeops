// SPDX-License-Identifier: FSL-1.1-ALv2
import { describe, expect, it } from "vitest";
import { validateNewRepoName } from "@/lib/repo-validation";

describe("validateNewRepoName", () => {
  const connectedOwner = "parag2-8487";

  it("accepts valid bare repository names and generates canonical name", () => {
    const res = validateNewRepoName("my-new-repo", connectedOwner);
    expect(res.error).toBeNull();
    expect(res.parsedName).toBe("my-new-repo");
    expect(res.canonicalName).toBe("parag2-8487/my-new-repo");
  });

  it("accepts valid explicit owner/repo matching connected account without duplication", () => {
    const res = validateNewRepoName("parag2-8487/my-new-repo", connectedOwner);
    expect(res.error).toBeNull();
    expect(res.parsedName).toBe("my-new-repo");
    expect(res.canonicalName).toBe("parag2-8487/my-new-repo");
  });

  it("handles case-insensitive owner matching", () => {
    const res = validateNewRepoName("PARAG2-8487/my-new-repo", connectedOwner);
    expect(res.error).toBeNull();
    expect(res.parsedName).toBe("my-new-repo");
    expect(res.canonicalName).toBe("parag2-8487/my-new-repo");
  });

  it("rejects repository when typed owner does not match connected account", () => {
    const res = validateNewRepoName("octocat/my-new-repo", connectedOwner);
    expect(res.error).toContain("does not match connected account @parag2-8487");
    expect(res.canonicalName).toBeNull();
  });

  it("rejects empty repository names", () => {
    expect(validateNewRepoName("", connectedOwner).error).toBe("Repository name cannot be empty.");
    expect(validateNewRepoName("   ", connectedOwner).error).toBe(
      "Repository name cannot be empty.",
    );
    expect(validateNewRepoName(null, connectedOwner).error).toBe(
      "Repository name cannot be empty.",
    );
    expect(validateNewRepoName(undefined, connectedOwner).error).toBe(
      "Repository name cannot be empty.",
    );
  });

  it("rejects repository names containing whitespace", () => {
    const res = validateNewRepoName("my new repo", connectedOwner);
    expect(res.error).toBe("Repository name cannot contain whitespace.");
  });

  it("rejects full GitHub URLs", () => {
    expect(
      validateNewRepoName("https://github.com/parag2-8487/my-repo", connectedOwner).error,
    ).toBe("Enter only the repository name, not a full GitHub URL.");
    expect(validateNewRepoName("http://github.com/parag2-8487/my-repo", connectedOwner).error).toBe(
      "Enter only the repository name, not a full GitHub URL.",
    );
    expect(validateNewRepoName("github.com/parag2-8487/my-repo", connectedOwner).error).toBe(
      "Enter only the repository name, not a full GitHub URL.",
    );
  });

  it("rejects repository names ending in .git", () => {
    const res = validateNewRepoName("my-repo.git", connectedOwner);
    expect(res.error).toBe("Repository name cannot end in .git.");
  });

  it("rejects consecutive slashes or leading/trailing slashes", () => {
    expect(validateNewRepoName("parag2-8487//my-repo", connectedOwner).error).toBe(
      "Consecutive slashes are forbidden.",
    );
    expect(validateNewRepoName("/my-repo", connectedOwner).error).toBe(
      "Repository name cannot start or end with a slash.",
    );
    expect(validateNewRepoName("my-repo/", connectedOwner).error).toBe(
      "Repository name cannot start or end with a slash.",
    );
  });

  it("rejects more than one slash", () => {
    const res = validateNewRepoName("a/b/c", connectedOwner);
    expect(res.error).toBe("Repository name cannot contain more than one slash.");
  });

  it("rejects names starting or ending with hyphen or period", () => {
    expect(validateNewRepoName("-my-repo", connectedOwner).error).toBe(
      "Repository name cannot start with a hyphen or period.",
    );
    expect(validateNewRepoName("my-repo-", connectedOwner).error).toBe(
      "Repository name cannot end with a hyphen or period.",
    );
    expect(validateNewRepoName(".my-repo", connectedOwner).error).toBe(
      "Repository name cannot start with a hyphen or period.",
    );
    expect(validateNewRepoName("my-repo.", connectedOwner).error).toBe(
      "Repository name cannot end with a hyphen or period.",
    );
  });

  it("rejects unsupported characters", () => {
    expect(validateNewRepoName("my$repo", connectedOwner).error).toContain(
      "only contain alphanumeric characters, hyphens, underscores, and periods",
    );
    expect(validateNewRepoName("my@repo", connectedOwner).error).toContain(
      "only contain alphanumeric characters, hyphens, underscores, and periods",
    );
    expect(validateNewRepoName("my repo!", connectedOwner).error).toContain(
      "Repository name cannot contain whitespace.",
    );
  });

  it("rejects repository names exceeding 100 characters", () => {
    const longName = "a".repeat(101);
    const res = validateNewRepoName(longName, connectedOwner);
    expect(res.error).toBe("Repository name cannot exceed 100 characters.");
  });
});
