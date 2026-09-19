// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

/**
 * Settings → Integrations: the per-user links to outside services.
 *
 * WHY HERE. A GitHub link belongs to the PERSON, not to a project — it outlives every project created
 * with it and it is revoked once rather than per project. So its home is a settings page where it can
 * be inspected and disconnected with no project in hand. It is ALSO surfaced on the create-project
 * form, because that is the one place where its absence blocks what the user is trying to do, and
 * sending them here to come back afterwards loses the form they were filling in.
 *
 * The page deliberately says what a link is NOT: sign-in is Authentik's, and nothing on this page
 * changes how anyone signs in.
 */

import { GitHubConnection } from "@/features/integrations/GitHubConnection";

export default function IntegrationsPage() {
  return (
    <main aria-labelledby="integrations-heading">
      <h1 id="integrations-heading">Integrations</h1>
      <p>
        Connections to outside services, held against your ForgeOps account. These are integrations,
        not sign-in methods — you continue to sign in through your organisation&apos;s identity
        provider.
      </p>
      <GitHubConnection />
    </main>
  );
}
