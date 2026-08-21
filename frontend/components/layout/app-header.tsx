// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useEffect, useState } from "react";
import { ThemeToggle } from "./theme-toggle";
import { SignOutButton } from "./sign-out-button";
import { getSession, subscribe } from "@/lib/session";

/**
 * The header shows WHO is signed in, and offers a way out.
 *
 * The subject is shown rather than a display name, because the subject is what the audit log records
 * against every action. A friendly name here and an opaque identifier in the audit trail would make
 * the two impossible to line up by eye, which is exactly when someone stops checking.
 */
export function AppHeader() {
  const [subject, setSubject] = useState<string | null>(null);

  useEffect(() => {
    const read = () => setSubject(getSession().user?.subject ?? null);
    read();
    return subscribe(read);
  }, []);

  return (
    <header className="flex h-14 items-center justify-between border-b px-6">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium text-muted-foreground md:hidden">ForgeOps</span>
      </div>
      <div className="flex items-center gap-3">
        {subject ? (
          <span className="hidden text-xs text-muted-foreground sm:inline" title="OIDC subject">
            Signed in as <code>{subject}</code>
          </span>
        ) : null}
        <ThemeToggle />
        <SignOutButton />
      </div>
    </header>
  );
}
