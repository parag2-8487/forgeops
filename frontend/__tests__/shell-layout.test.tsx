import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AppSidebar } from "@/components/layout/app-sidebar";
import { AppHeader } from "@/components/layout/app-header";
import { ThemeToggle } from "@/components/layout/theme-toggle";

// Mock next/navigation
let mockPathname = "/";
vi.mock("next/navigation", () => ({
  usePathname: () => mockPathname,
}));

// Mock next-themes
const mockSetTheme = vi.fn();
let mockTheme = "light";
vi.mock("next-themes", () => ({
  useTheme: () => ({ theme: mockTheme, setTheme: mockSetTheme }),
  ThemeProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

// Mock next/link to render a plain anchor
vi.mock("next/link", () => ({
  default: ({
    children,
    href,
    ...props
  }: {
    children: React.ReactNode;
    href: string;
  } & React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

describe("AppSidebar", () => {
  it("renders exactly one navigation link: Home -> /", () => {
    render(<AppSidebar />);
    const nav = screen.getByRole("navigation", { name: "Primary" });
    const links = nav.querySelectorAll("a");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "/");
    expect(links[0]).toHaveTextContent("Home");
  });

  it("has no disabled or placeholder future links", () => {
    render(<AppSidebar />);
    const nav = screen.getByRole("navigation", { name: "Primary" });
    const disabledLinks = nav.querySelectorAll('[aria-disabled="true"], [disabled]');
    expect(disabledLinks).toHaveLength(0);
    // No other links exist at all
    const allLinks = nav.querySelectorAll("a");
    expect(allLinks).toHaveLength(1);
  });

  it("sets aria-current=page at / pathname", () => {
    mockPathname = "/";
    render(<AppSidebar />);
    const link = screen.getByRole("link", { name: /Home/i });
    expect(link).toHaveAttribute("aria-current", "page");
  });

  it("does NOT set aria-current=page at other pathnames", () => {
    mockPathname = "/other";
    render(<AppSidebar />);
    const link = screen.getByRole("link", { name: /Home/i });
    expect(link).not.toHaveAttribute("aria-current");
  });

  it("applies active styling at / pathname", () => {
    mockPathname = "/";
    render(<AppSidebar />);
    const link = screen.getByRole("link", { name: /Home/i });
    expect(link.className).toContain("bg-sidebar-accent");
  });

  it("link has focus-visible ring classes for keyboard accessibility", () => {
    render(<AppSidebar />);
    const link = screen.getByRole("link", { name: /Home/i });
    expect(link.className).toContain("focus-visible:ring-2");
  });

  it("link is keyboard reachable and activatable", async () => {
    render(<AppSidebar />);
    const user = userEvent.setup();
    const link = screen.getByRole("link", { name: /Home/i });

    await user.tab();
    expect(link).toHaveFocus();
  });

  it("renders nav landmark with aria-label Primary", () => {
    render(<AppSidebar />);
    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(nav).toBeInTheDocument();
  });
});

describe("AppHeader", () => {
  it("renders the header", () => {
    render(<AppHeader />);
    const header = screen.getByRole("banner");
    expect(header).toBeInTheDocument();
  });

  it("contains the theme toggle button", () => {
    render(<AppHeader />);
    const button = screen.getByRole("button", { name: /theme/i });
    expect(button).toBeInTheDocument();
  });
});

describe("ThemeToggle", () => {
  it("renders as a button with accessible label", () => {
    render(<ThemeToggle />);
    const button = screen.getByRole("button", { name: /theme/i });
    expect(button).toBeInTheDocument();
  });

  it("calls setTheme on click", async () => {
    mockTheme = "light";
    render(<ThemeToggle />);
    const user = userEvent.setup();
    const button = screen.getByRole("button", { name: /theme/i });
    await user.click(button);
    expect(mockSetTheme).toHaveBeenCalledWith("dark");
  });
});
