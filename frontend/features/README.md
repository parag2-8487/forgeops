# `features/` — per-feature UI modules

Required by the PRD §8 layout. Phase 0 left this directory deliberately empty; Phase 1 filled
it.

**This file used to say the opposite, and was wrong.** It read "structural artifact only. This
directory contains no feature placeholder — no `.ts`, `.tsx`, `.js`, or `.jsx` file, and no
per-feature subdirectory" while eight subdirectories each holding a `.tsx` component sat beside
it. That was true under the Phase 0 rule (design §1.3) and stopped being true when the modules
landed, and nothing updated it. Recorded rather than silently replaced, because a directory
whose README denies its own contents is worth remembering.

## What is here, and what state each module is in

Every module is now reachable from a route under `app/(shell)/` and from the sidebar. Before
2026-08-21 none of them was: the app had exactly one page, which rendered a hardcoded array.

| Module                           | State                                 | Route         |
| :------------------------------- | :------------------------------------ | :------------ |
| `projects/ProjectList.tsx`       | props-driven; fed live                | `/projects`   |
| `readiness/RadarChart.tsx`       | props-driven; fed live                | `/readiness`  |
| `audit/AuditViewer.tsx`          | **rewritten** to props; fed live      | `/audit`      |
| `vault/SecretVault.tsx`          | **rewritten** to props; fed live      | `/vault`      |
| `approvals/ApprovalCenter.tsx`   | props-driven, display-only; unmounted | `/approvals`  |
| `policies/PolicyEditor.tsx`      | inert — save control does nothing     | `/policies`   |
| `generation/GeneratorWizard.tsx` | inert — no submit handler             | `/generation` |
| `pairing/AgentPairing.tsx`       | hardcoded status claim; unmounted     | `/pairing`    |

`AuditViewer` and `SecretVault` were rewritten because they contained **fabricated data** — two
invented log entries and a secret reference for a `DATABASE_PASSWORD` that does not exist. They
take real records now.

The four modules that are unmounted or inert are not rendered on their routes. Those routes show
an explicit not-implemented panel naming what is missing and which phase owns it, because a
screen that looks finished and does nothing is harder to plan around than a blank one. See
`components/ui/not-implemented.tsx`.

`readiness/RadarChart.tsx` is a misnomer worth knowing about: it exports `ReadinessRadarChart`
and draws horizontal bars, not a radar plot.
