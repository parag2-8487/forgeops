import { AppSidebar } from "@/components/layout/app-sidebar";
import { AuthBoundary } from "@/components/layout/auth-boundary";
import { AppHeader } from "@/components/layout/app-header";

export default function ShellLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-dvh">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:p-2 focus:bg-background focus:text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
      >
        Skip to main content
      </a>
      <AppSidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <AppHeader />
        <main id="main" tabIndex={-1} className="flex-1 overflow-y-auto p-6">
          {/* Inside <main> rather than around the whole shell, so the sidebar and header stay
              rendered while the session is being restored -- the chrome is not what needs
              guarding, the panels that fetch are. */}
          <AuthBoundary>{children}</AuthBoundary>
        </main>
      </div>
    </div>
  );
}
