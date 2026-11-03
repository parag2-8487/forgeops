// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * Strict Git branch ref validation mirroring backend/src/deployments/autonomous_schemas.py.
 */

export function validateGitBranchName(branch: string | null | undefined): string | null {
  if (branch === null || branch === undefined) {
    return null;
  }
  if (branch.length === 0) {
    return "Branch name cannot be empty.";
  }
  if (/\s/.test(branch)) {
    return `Invalid branch name '${branch}': whitespace is forbidden.`;
  }
  if (branch.startsWith("-")) {
    return `Invalid branch name '${branch}': cannot begin with a hyphen '-' (git option injection prevention).`;
  }
  const encoder = new TextEncoder();
  const encoded = encoder.encode(branch);
  if (encoded.length > 255) {
    return `Branch name exceeds maximum length of 255 bytes (got ${encoded.length} bytes).`;
  }
  if (branch === "@" || branch.startsWith("/") || branch.endsWith("/") || branch.endsWith(".")) {
    return `Invalid branch name '${branch}': cannot be '@', start/end with '/', or end with '.'.`;
  }
  if (branch.includes("//")) {
    return `Invalid branch name '${branch}': consecutive slashes '//' are forbidden.`;
  }
  if (branch.includes("..") || branch.includes("@{") || branch.includes("\\")) {
    return `Invalid branch name '${branch}': contains forbidden sequence ('..', '@{', or '\\').`;
  }
  for (let i = 0; i < branch.length; i++) {
    const code = branch.charCodeAt(i);
    const ch = branch[i];
    if (code < 32 || code === 127 || "~^:?*[".includes(ch)) {
      return `Invalid branch name '${branch}': contains forbidden character '${ch}'.`;
    }
  }
  const comps = branch.split("/");
  for (const comp of comps) {
    if (!comp) {
      return `Invalid branch name '${branch}': empty path component.`;
    }
    if (comp.startsWith(".")) {
      return `Invalid branch name '${branch}': path component '${comp}' cannot start with '.'.`;
    }
    if (comp.endsWith(".lock")) {
      return `Invalid branch name '${branch}': path component '${comp}' cannot end with '.lock'.`;
    }
  }
  return null;
}
