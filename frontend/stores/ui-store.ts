import { create } from "zustand";
import { persist } from "zustand/middleware";

interface UiState {
  sidebarCollapsed: boolean;
  commandPaletteOpen: boolean;
  toggleSidebar: () => void;
  setCommandPaletteOpen: (open: boolean) => void;
}

/**
 * Client-only UI state. Never holds server data — that belongs in TanStack Query.
 * Persisted to localStorage so the sidebar state survives page refreshes.
 */
export const useUiStore = create<UiState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      commandPaletteOpen: false,
      toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setCommandPaletteOpen: (open: boolean) => set({ commandPaletteOpen: open }),
    }),
    { name: "ui", version: 1 },
  ),
);
