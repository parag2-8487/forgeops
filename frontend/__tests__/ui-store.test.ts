import { describe, expect, it, beforeEach } from "vitest";
import { useUiStore } from "@/stores/ui-store";

describe("ui-store", () => {
  beforeEach(() => {
    // Reset store state between tests
    useUiStore.setState({
      sidebarCollapsed: false,
      commandPaletteOpen: false,
    });
  });

  it("initializes with sidebar expanded and command palette closed", () => {
    const state = useUiStore.getState();
    expect(state.sidebarCollapsed).toBe(false);
    expect(state.commandPaletteOpen).toBe(false);
  });

  it("toggleSidebar flips the collapsed state", () => {
    useUiStore.getState().toggleSidebar();
    expect(useUiStore.getState().sidebarCollapsed).toBe(true);
    useUiStore.getState().toggleSidebar();
    expect(useUiStore.getState().sidebarCollapsed).toBe(false);
  });

  it("setCommandPaletteOpen sets the state", () => {
    useUiStore.getState().setCommandPaletteOpen(true);
    expect(useUiStore.getState().commandPaletteOpen).toBe(true);
    useUiStore.getState().setCommandPaletteOpen(false);
    expect(useUiStore.getState().commandPaletteOpen).toBe(false);
  });

  it("contains ONLY client UI state, no server-derived data fields", () => {
    const state = useUiStore.getState();
    const stateKeys = Object.keys(state).filter(
      (k) => typeof state[k as keyof typeof state] !== "function",
    );
    // Only these two state fields should exist (no health, projects, etc.)
    expect(stateKeys.sort()).toEqual(["commandPaletteOpen", "sidebarCollapsed"]);
  });

  it("persists state via the persist middleware config", () => {
    // The store is configured with persist middleware - verify the name
    const persistOptions = (useUiStore as any).persist;
    expect(persistOptions).toBeDefined();
    expect(persistOptions.getOptions().name).toBe("ui");
    expect(persistOptions.getOptions().version).toBe(1);
  });
});
