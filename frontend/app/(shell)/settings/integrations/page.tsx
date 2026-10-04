// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

/**
 * Settings → Integrations: the per-user links to outside services.
 */

import { GitHubConnection } from "@/features/integrations/GitHubConnection";

export default function IntegrationsPage() {
  return (
    <main aria-labelledby="integrations-heading" className="max-w-4xl space-y-6">
      <div>
        <h1 id="integrations-heading" className="text-2xl font-bold tracking-tight">
          Integrations
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Connections to outside services, held against your ForgeOps account. These are
          integrations, not sign-in methods — you continue to sign in through your
          organisation&apos;s identity provider.
        </p>
      </div>

      <GitHubConnection />
    </main>
  );
}
