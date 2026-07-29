"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Home } from "lucide-react";
import { cn } from "@/lib/utils";

export function AppSidebar() {
  const pathname = usePathname();

  return (
    <aside className="hidden w-64 shrink-0 border-r bg-sidebar-background md:block">
      <div className="flex h-14 items-center border-b px-4">
        <span className="text-lg font-semibold text-sidebar-foreground">ForgeOps</span>
      </div>
      <nav aria-label="Primary" className="p-2">
        <Link
          href="/"
          aria-current={pathname === "/" ? "page" : undefined}
          className={cn(
            "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring",
            pathname === "/"
              ? "bg-sidebar-accent text-sidebar-accent-foreground"
              : "text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
          )}
        >
          <Home className="h-4 w-4" />
          Home
        </Link>
      </nav>
    </aside>
  );
}
