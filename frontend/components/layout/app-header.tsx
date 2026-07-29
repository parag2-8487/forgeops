import { ThemeToggle } from "./theme-toggle";

export function AppHeader() {
  return (
    <header className="flex h-14 items-center justify-between border-b px-6">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium text-muted-foreground md:hidden">ForgeOps</span>
      </div>
      <div className="flex items-center gap-2">
        <ThemeToggle />
      </div>
    </header>
  );
}
