"use client";

/**
 * The incidents page. Phase 2 2.11.
 *
 * The list and the detail are one page with local selection rather than two routes, because an operator
 * triaging an outage moves between rows constantly and a full navigation per row loses the list's scroll
 * position and its refresh cycle.
 */

import { useState } from "react";

import { IncidentDetail, IncidentList } from "@/features/incidents/IncidentPanels";

export default function IncidentsPage() {
  const [selected, setSelected] = useState<string | null>(null);
  // Read from the URL rather than an env var: a build-time default project would be wrong on every
  // deployment but one, and adding an env key for it would make the page depend on configuration to
  // show anything at all.
  const projectId =
    typeof window === "undefined"
      ? ""
      : (new URLSearchParams(window.location.search).get("project") ?? "");

  return (
    <main aria-labelledby="incidents-page-heading">
      <h1 id="incidents-page-heading">Troubleshooting</h1>
      {projectId ? (
        <>
          <IncidentList projectId={projectId} onSelect={setSelected} />
          {selected ? <IncidentDetail incidentId={selected} /> : null}
        </>
      ) : (
        <p>
          No project is selected, so there is nothing to show. This page reports incidents for one
          project at a time; it is not showing an empty list for every project.
        </p>
      )}
    </main>
  );
}
