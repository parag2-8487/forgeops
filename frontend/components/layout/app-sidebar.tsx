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
} from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Every route in the app, in one list.
 *
 * `live` records whether the route reads from a real backend endpoint. It is not decoration: keeping
 * the flag here means the nav and the pages cannot disagree about which is which.
 *
 * All nine are now `true`. Approvals, Generation and Pairing were `false` while their backend
 * surface did not exist, and each rendered an explicit not-implemented panel rather than sample
 * data. They are served now -- approvals reads `GET /api/v1/approvals`, generation streams the six
 * §7.4 event types from `POST /api/v1/generation/runs`, and pairing queries the device list -- and
 * `NotImplemented` is no longer imported anywhere under `app/`. The flag said otherwise for longer
 * than it was true, which is the failure mode a comment like this one is supposed to prevent.
 *
 * The eight feature modules under `features/` were built and then mounted on nothing — the app
 * had exactly one page, rendering a hardcoded array. This list is what mounts them.
 */
const NAV_ITEMS = [
  { href: "/", label: "Home", icon: Home, live: true },
  { href: "/projects", label: "Projects", icon: FolderGit2, live: true },
  { href: "/readiness", label: "Readiness", icon: GaugeCircle, live: true },
  { href: "/audit", label: "Audit", icon: ScrollText, live: true },
  { href: "/policies", label: "Policies", icon: ShieldCheck, live: true },
  { href: "/vault", label: "Vault", icon: KeyRound, live: true },
  { href: "/approvals", label: "Approvals", icon: GitPullRequestArrow, live: true },
  { href: "/generation", label: "Generation", icon: Wand2, live: true },
  { href: "/pairing", label: "Pairing", icon: Radio, live: true },
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
