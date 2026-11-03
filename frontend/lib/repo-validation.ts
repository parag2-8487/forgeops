// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * Strict repository name validation and canonicalization mirroring
 * backend/src/deployments/autonomous_schemas.py and backend/src/integrations/routes.py.
 */

export interface RepoValidationResult {
  error: string | null;
  parsedName: string;
  canonicalName: string | null;
}

export function validateNewRepoName(
  raw: string | null | undefined,
  connectedOwner?: string | null,
): RepoValidationResult {
  if (!raw || !raw.trim()) {
    return {
      error: "Repository name cannot be empty.",
      parsedName: "",
      canonicalName: null,
    };
  }

  const trimmed = raw.trim();

  // Reject whitespace inside
  if (/\s/.test(trimmed)) {
    return {
      error: "Repository name cannot contain whitespace.",
      parsedName: trimmed,
      canonicalName: null,
    };
  }

  // Reject full GitHub URLs
  if (
    trimmed.startsWith("http://") ||
    trimmed.startsWith("https://") ||
    trimmed.includes("github.com")
  ) {
    return {
      error: "Enter only the repository name, not a full GitHub URL.",
      parsedName: trimmed,
      canonicalName: null,
    };
  }

  // Reject .git suffix
  if (trimmed.endsWith(".git")) {
    return {
      error: "Repository name cannot end in .git.",
      parsedName: trimmed,
      canonicalName: null,
    };
  }

  // Slashes and path structure
  if (trimmed.includes("//")) {
    return {
      error: "Consecutive slashes are forbidden.",
      parsedName: trimmed,
      canonicalName: null,
    };
  }

  if (trimmed.startsWith("/") || trimmed.endsWith("/")) {
    return {
      error: "Repository name cannot start or end with a slash.",
      parsedName: trimmed,
      canonicalName: null,
    };
  }

  const parts = trimmed.split("/");
  if (parts.length > 2) {
    return {
      error: "Repository name cannot contain more than one slash.",
      parsedName: trimmed,
      canonicalName: null,
    };
  }

  let repoComponent = trimmed;
  if (parts.length === 2) {
    const ownerPart = parts[0];
    repoComponent = parts[1];

    if (!repoComponent) {
      return {
        error: "Repository name cannot be empty.",
        parsedName: trimmed,
        canonicalName: null,
      };
    }

    if (connectedOwner && ownerPart.toLowerCase() !== connectedOwner.toLowerCase()) {
      return {
        error: `Repository owner '${ownerPart}' does not match connected account @${connectedOwner}.`,
        parsedName: repoComponent,
        canonicalName: null,
      };
    }
  }

  // Component character set: only letters, numbers, hyphens, underscores, and periods
  const validPattern = /^[A-Za-z0-9_.-]+$/;
  if (!validPattern.test(repoComponent)) {
    return {
      error:
        "Repository name can only contain alphanumeric characters, hyphens, underscores, and periods.",
      parsedName: repoComponent,
      canonicalName: null,
    };
  }

  if (repoComponent.startsWith("-") || repoComponent.startsWith(".")) {
    return {
      error: "Repository name cannot start with a hyphen or period.",
      parsedName: repoComponent,
      canonicalName: null,
    };
  }

  if (repoComponent.endsWith("-") || repoComponent.endsWith(".")) {
    return {
      error: "Repository name cannot end with a hyphen or period.",
      parsedName: repoComponent,
      canonicalName: null,
    };
  }

  if (repoComponent.length > 100) {
    return {
      error: "Repository name cannot exceed 100 characters.",
      parsedName: repoComponent,
      canonicalName: null,
    };
  }

  const canonical = connectedOwner ? `${connectedOwner}/${repoComponent}` : repoComponent;

  return {
    error: null,
    parsedName: repoComponent,
    canonicalName: canonical,
  };
}
