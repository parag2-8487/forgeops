"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Home,
  FolderGit2,
  GaugeCircle,
  ScrollText,
  ShieldCheck,
  KeyRound,
  GitPullRequestArrow,
  Wand2,
  Radio,
  Rocket,
  Cpu,
  FileSearch,
} from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Every route in the app, in one list.
 *
 * `live` recorded whether the route reads from a real backend endpoint. IT IS GONE, and its removal is
 * the point of this comment. The flag existed because three screens rendered explicit
 * not-implemented panels while their backend surface did not exist, and it outlived that: it said all
 * nine were `true` and then stopped being consulted for anything, because every route reads real data
 * and the pages themselves state what they can and cannot observe. A boolean that is `true` for every
 * row carries no information and is one edit away from being wrong again, which is exactly the failure
 * mode the comment it replaced was written to warn about. Where a screen genuinely cannot know
 * something — whether a policy bundle is published, for one — it says so on its own face, which is a
 * claim that cannot drift out of step with a list in the navigation.
 *
 * Three routes are new. `Getting started` is the ordered path from an empty database to an applied
 * change, which nothing previously described; `Plan analysis` and `Model tiers` surface two endpoints
 * that were served, tested and called by nothing.
 */
const NAV_ITEMS = [
  { href: "/", label: "Home", icon: Home },
  { href: "/onboarding", label: "Getting started", icon: Rocket },
  { href: "/projects", label: "Projects", icon: FolderGit2 },
  { href: "/readiness", label: "Readiness", icon: GaugeCircle },
  { href: "/generation", label: "Generation", icon: Wand2 },
  { href: "/approvals", label: "Approvals", icon: GitPullRequestArrow },
  { href: "/policies", label: "Policies", icon: ShieldCheck },
  { href: "/vault", label: "Vault", icon: KeyRound },
  { href: "/pairing", label: "Pairing", icon: Radio },
  { href: "/analysis", label: "Plan analysis", icon: FileSearch },
  { href: "/models", label: "Model tiers", icon: Cpu },
  { href: "/audit", label: "Audit", icon: ScrollText },
] as const;

export function AppSidebar() {
  const pathname = usePathname();

  return (
    <aside className="hidden w-64 shrink-0 border-r bg-sidebar-background md:block">
      <div className="flex h-14 items-center border-b px-4">
        <span className="text-lg font-semibold text-sidebar-foreground">ForgeOps</span>
      </div>
      <nav aria-label="Primary" className="p-2">
        {NAV_ITEMS.map(({ href, label, icon: Icon }) => {
          const active = pathname === href;
          return (
            <Link
              key={href}
              href={href}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring",
                active
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
              )}
            >
              <Icon className="h-4 w-4" />
              {label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
