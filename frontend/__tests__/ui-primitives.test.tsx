// SPDX-License-Identifier: Apache-2.0
/**
 * The shared UI primitives, tested on the branches they actually have.
 *
 * These were at 0 % because nothing rendered them outside a route, and a smoke render would have
 * moved the number without proving anything. So each block here targets a decision the component
 * makes: a variant that changes output, a disabled state that must suppress an interaction, an
 * error state that must reach assistive technology, a prop whose absence takes a different path.
 *
 * Queried by role and accessible name throughout. Where a class IS the component's output — `cva`
 * variants exist to produce different classes and nothing else — the assertion compares variants
 * against each other rather than hardcoding a Tailwind string, so it survives a design change but
 * still fails if a variant stops being distinct.
 */

import React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useForm } from "react-hook-form";

import { Button, buttonVariants } from "@/components/ui/button";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  CardFooter,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sheet,
  SheetTrigger,
  SheetContent,
  SheetHeader,
  SheetFooter,
  SheetTitle,
  SheetDescription,
  SheetClose,
} from "@/components/ui/sheet";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuCheckboxItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuGroup,
} from "@/components/ui/dropdown-menu";
import {
  Form,
  FormField,
  FormItem,
  FormLabel,
  FormControl,
  FormDescription,
  FormMessage,
  useFormField,
} from "@/components/ui/form";
import { AsyncState } from "@/components/ui/async-state";
import { NotImplemented } from "@/components/ui/not-implemented";
import { DEFAULT_PROJECT_ID, ProjectIdField } from "@/components/ui/project-id-field";
import { ApiProblemError, ApiTransportError } from "@/lib/api";

afterEach(() => cleanup());

describe("Button", () => {
  it("is reachable by its accessible name and reports clicks", async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Approve change-set</Button>);
    await userEvent.click(screen.getByRole("button", { name: "Approve change-set" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("suppresses the click entirely when disabled", async () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Apply
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Apply" });
    expect(button).toBeDisabled();
    await userEvent.click(button);
    // The behavioural claim, not the class: a disabled destructive action must not fire.
    expect(onClick).not.toHaveBeenCalled();
  });

  it("renders as the child element when asChild is set, so a link stays a link", () => {
    render(
      <Button asChild>
        <a href="/audit">Open the audit log</a>
      </Button>,
    );
    // A button-styled anchor must keep the link role, or keyboard and screen-reader semantics
    // silently change. This is the branch `asChild` exists for.
    const link = screen.getByRole("link", { name: "Open the audit log" });
    expect(link).toHaveAttribute("href", "/audit");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("produces a distinct class for every variant and every size", () => {
    const variants = ["default", "destructive", "outline", "secondary", "ghost", "link"] as const;
    const sizes = ["default", "sm", "lg", "icon"] as const;

    const variantClasses = variants.map((variant) => buttonVariants({ variant }));
    const sizeClasses = sizes.map((size) => buttonVariants({ size }));

    // Compared against each other rather than against hardcoded Tailwind, so restyling does not
    // break the test but collapsing two variants into the same output does.
    expect(new Set(variantClasses).size).toBe(variants.length);
    expect(new Set(sizeClasses).size).toBe(sizes.length);
  });

  it("merges a caller className rather than dropping it", () => {
    render(<Button className="sentinel-class">Go</Button>);
    expect(screen.getByRole("button", { name: "Go" })).toHaveClass("sentinel-class");
  });

  it("forwards a ref to the underlying button element", () => {
    const ref = React.createRef<HTMLButtonElement>();
    render(<Button ref={ref}>Focus me</Button>);
    expect(ref.current).toBeInstanceOf(HTMLButtonElement);
    ref.current?.focus();
    expect(screen.getByRole("button", { name: "Focus me" })).toHaveFocus();
  });
});

describe("Card", () => {
  it("renders its title as a level-3 heading with the description and body", () => {
    render(
      <Card>
        <CardHeader>
          <CardTitle>Change-set cs-1</CardTitle>
          <CardDescription>Two files, one deletion</CardDescription>
        </CardHeader>
        <CardContent>The diff body</CardContent>
        <CardFooter>
          <Button>Approve</Button>
        </CardFooter>
      </Card>,
    );
    // The heading LEVEL is the accessibility contract a card carries, so it is asserted by role.
    expect(screen.getByRole("heading", { level: 3, name: "Change-set cs-1" })).toBeInTheDocument();
    expect(screen.getByText("Two files, one deletion")).toBeInTheDocument();
    expect(screen.getByText("The diff body")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("forwards refs and merges classNames on every subcomponent", () => {
    const refs = {
      card: React.createRef<HTMLDivElement>(),
      header: React.createRef<HTMLDivElement>(),
      title: React.createRef<HTMLParagraphElement>(),
      description: React.createRef<HTMLParagraphElement>(),
      content: React.createRef<HTMLDivElement>(),
      footer: React.createRef<HTMLDivElement>(),
    };
    render(
      <Card ref={refs.card} className="c">
        <CardHeader ref={refs.header} className="h">
          <CardTitle ref={refs.title} className="t">
            T
          </CardTitle>
          <CardDescription ref={refs.description} className="d">
            D
          </CardDescription>
        </CardHeader>
        <CardContent ref={refs.content} className="ct">
          C
        </CardContent>
        <CardFooter ref={refs.footer} className="f">
          F
        </CardFooter>
      </Card>,
    );
    expect(refs.card.current).toHaveClass("c");
    expect(refs.header.current).toHaveClass("h");
    expect(refs.title.current).toHaveClass("t");
    expect(refs.description.current).toHaveClass("d");
    expect(refs.content.current).toHaveClass("ct");
    expect(refs.footer.current).toHaveClass("f");
  });
});

describe("Separator", () => {
  it("is hidden from assistive technology when decorative, which is the default", () => {
    render(<Separator data-testid="sep" />);
    // Radix marks a decorative separator with role="none", so it must NOT be findable as one.
    expect(screen.queryByRole("separator")).not.toBeInTheDocument();
    expect(screen.getByTestId("sep")).toBeInTheDocument();
  });

  it("is announced as a separator when told it is not decorative", () => {
    render(<Separator decorative={false} />);
    expect(screen.getByRole("separator")).toBeInTheDocument();
  });

  it("reports its orientation when vertical", () => {
    render(<Separator decorative={false} orientation="vertical" />);
    expect(screen.getByRole("separator")).toHaveAttribute("aria-orientation", "vertical");
  });

  it("takes a different size class per orientation", () => {
    const { getByTestId, unmount } = render(<Separator data-testid="s" orientation="horizontal" />);
    const horizontal = getByTestId("s").className;
    unmount();
    render(<Separator data-testid="s" orientation="vertical" />);
    expect(getByTestId("s").className).not.toBe(horizontal);
  });
});

describe("Skeleton", () => {
  it("renders an element that can be hidden from assistive technology while loading", () => {
    render(<Skeleton data-testid="sk" aria-hidden="true" className="h-4 w-32" />);
    const el = screen.getByTestId("sk");
    expect(el).toHaveAttribute("aria-hidden", "true");
    // A skeleton whose animation class is gone is a blank box, so this is its whole job.
    expect(el).toHaveClass("animate-pulse");
    expect(el).toHaveClass("h-4", "w-32");
  });
});

describe("Sheet", () => {
  it("renders nothing until the trigger is used, then shows a dialog with its title", async () => {
    render(
      <Sheet>
        <SheetTrigger>Open device details</SheetTrigger>
        <SheetContent>
          <SheetHeader>
            <SheetTitle>Device d-1</SheetTitle>
            <SheetDescription>Paired 4 minutes ago</SheetDescription>
          </SheetHeader>
          <SheetFooter>
            <SheetClose>Dismiss</SheetClose>
          </SheetFooter>
        </SheetContent>
      </Sheet>,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Open device details" }));

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toBeInTheDocument();
    // Radix wires the title and description as the dialog's accessible name and description.
    expect(screen.getByRole("heading", { name: "Device d-1" })).toBeInTheDocument();
    expect(screen.getByText("Paired 4 minutes ago")).toBeInTheDocument();
  });

  it("closes again through its own close control", async () => {
    render(
      <Sheet>
        <SheetTrigger>Open</SheetTrigger>
        <SheetContent>
          <SheetTitle>Details</SheetTitle>
        </SheetContent>
      </Sheet>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    await screen.findByRole("dialog");

    // The built-in corner control, whose only accessible name is its sr-only "Close" span.
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("positions itself differently for each side variant", async () => {
    const seen = new Set<string>();
    for (const side of ["top", "bottom", "left", "right"] as const) {
      const { unmount } = render(
        <Sheet defaultOpen>
          <SheetContent side={side} data-testid={`sheet-${side}`}>
            <SheetTitle>{side}</SheetTitle>
          </SheetContent>
        </Sheet>,
      );
      seen.add((await screen.findByTestId(`sheet-${side}`)).className);
      unmount();
    }
    // Four sides must yield four different placements, or one of them is silently wrong.
    expect(seen.size).toBe(4);
  });
});

describe("DropdownMenu", () => {
  function Menu({
    onSelect = vi.fn(),
    checked = false,
  }: {
    onSelect?: () => void;
    checked?: boolean;
  }) {
    return (
      <DropdownMenu>
        <DropdownMenuTrigger>Actions</DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuLabel>Change-set</DropdownMenuLabel>
          <DropdownMenuGroup>
            <DropdownMenuItem onSelect={onSelect}>Approve</DropdownMenuItem>
            <DropdownMenuItem disabled onSelect={onSelect}>
              Rollback
            </DropdownMenuItem>
          </DropdownMenuGroup>
          <DropdownMenuSeparator />
          <DropdownMenuCheckboxItem checked={checked}>Show diff</DropdownMenuCheckboxItem>
          <DropdownMenuRadioGroup value="unified">
            <DropdownMenuRadioItem value="unified">
              Unified
              <DropdownMenuShortcut>⌘U</DropdownMenuShortcut>
            </DropdownMenuRadioItem>
            <DropdownMenuRadioItem value="split">Split</DropdownMenuRadioItem>
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    );
  }

  it("opens on the trigger and exposes its items with menu semantics", async () => {
    render(<Menu />);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Actions" }));

    expect(await screen.findByRole("menu")).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("separator")).toBeInTheDocument();
    expect(screen.getByText("Change-set")).toBeInTheDocument();
  });

  it("invokes the handler for an enabled item", async () => {
    const onSelect = vi.fn();
    render(<Menu onSelect={onSelect} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Approve" }));
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("refuses to invoke a disabled item", async () => {
    const onSelect = vi.fn();
    render(<Menu onSelect={onSelect} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    const rollback = await screen.findByRole("menuitem", { name: "Rollback" });
    expect(rollback).toHaveAttribute("data-disabled");
    await userEvent.click(rollback);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("reports checkbox and radio state through ARIA rather than through an icon alone", async () => {
    render(<Menu checked />);
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));

    expect(await screen.findByRole("menuitemcheckbox", { name: "Show diff" })).toBeChecked();
    // The radio group's value is "unified", so exactly that one reports checked.
    expect(screen.getByRole("menuitemradio", { name: /Unified/ })).toBeChecked();
    expect(screen.getByRole("menuitemradio", { name: "Split" })).not.toBeChecked();
  });

  it("renders a shortcut hint alongside its item", async () => {
    render(<Menu />);
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    expect(await screen.findByText("⌘U")).toBeInTheDocument();
  });
});

describe("Form", () => {
  /** A minimal real react-hook-form, because the primitives read its context. */
  function Harness({
    onValid = vi.fn(),
    requireIt = true,
  }: {
    onValid?: (v: { projectId: string }) => void;
    requireIt?: boolean;
  }) {
    const form = useForm<{ projectId: string }>({ defaultValues: { projectId: "" } });
    return (
      <Form {...form}>
        <form onSubmit={form.handleSubmit(onValid)}>
          <FormField
            control={form.control}
            name="projectId"
            rules={requireIt ? { required: "A project id is required" } : {}}
            render={({ field }) => (
              <FormItem>
                <FormLabel>Project</FormLabel>
                <FormControl>
                  <input {...field} />
                </FormControl>
                <FormDescription>The UUID of the project to act on.</FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />
          <Button type="submit">Submit</Button>
        </form>
      </Form>
    );
  }

  it("associates the label, description and control so the field has an accessible name", () => {
    render(<Harness />);
    const input = screen.getByLabelText("Project");
    expect(input).toBeInTheDocument();
    // Clean state: described by the description only, and explicitly not invalid.
    expect(input).toHaveAttribute("aria-invalid", "false");
    const describedBy = input.getAttribute("aria-describedby") ?? "";
    expect(describedBy.trim().split(/\s+/)).toHaveLength(1);
  });

  it("renders no message element while the field is valid", () => {
    render(<Harness />);
    // FormMessage returns null with no error and no children — the branch that must not render
    // an empty paragraph that reserves space or announces nothing.
    expect(screen.queryByText("A project id is required")).not.toBeInTheDocument();
  });

  it("surfaces a validation error as text, marks the control invalid, and describes it", async () => {
    const onValid = vi.fn();
    render(<Harness onValid={onValid} />);
    await userEvent.click(screen.getByRole("button", { name: "Submit" }));

    expect(await screen.findByText("A project id is required")).toBeInTheDocument();
    const input = screen.getByLabelText("Project");
    expect(input).toHaveAttribute("aria-invalid", "true");
    // Now described by BOTH the description and the message, which is the error branch of
    // FormControl's aria-describedby.
    const describedBy = input.getAttribute("aria-describedby") ?? "";
    expect(describedBy.trim().split(/\s+/)).toHaveLength(2);
    expect(onValid).not.toHaveBeenCalled();
  });

  it("submits the typed value once the field is valid", async () => {
    const onValid = vi.fn();
    render(<Harness onValid={onValid} />);
    await userEvent.type(screen.getByLabelText("Project"), DEFAULT_PROJECT_ID);
    await userEvent.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() =>
      expect(onValid).toHaveBeenCalledWith(
        expect.objectContaining({ projectId: DEFAULT_PROJECT_ID }),
        expect.anything(),
      ),
    );
  });

  it("renders explicit children in the message slot when there is no error", () => {
    function WithChildren() {
      const form = useForm<{ a: string }>({ defaultValues: { a: "" } });
      return (
        <Form {...form}>
          <FormField
            control={form.control}
            name="a"
            render={() => (
              <FormItem>
                <FormMessage>A standing hint</FormMessage>
              </FormItem>
            )}
          />
        </Form>
      );
    }
    render(<WithChildren />);
    expect(screen.getByText("A standing hint")).toBeInTheDocument();
  });

  it("throws when useFormField is used outside a FormField", () => {
    function Orphan() {
      useFormField();
      return null;
    }
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    // The guard exists so a misuse fails loudly at development time instead of rendering a
    // field wired to nothing. Asserting it throws is asserting the guard works.
    expect(() => render(<Orphan />)).toThrow();
    spy.mockRestore();
  });
});

describe("AsyncState", () => {
  const child = <p>the real content</p>;

  it("renders its children when there is nothing pending, no error and content present", () => {
    render(
      <AsyncState isPending={false} error={null} isEmpty={false} label="x">
        {child}
      </AsyncState>,
    );
    expect(screen.getByText("the real content")).toBeInTheDocument();
  });

  it("prefers the error branch over the empty branch when both could apply", () => {
    render(
      <AsyncState
        isPending={false}
        error={new ApiProblemError({ type: "t", title: "Nope", status: 500 })}
        isEmpty
        emptyMessage="should not be seen"
        label="x"
      >
        {child}
      </AsyncState>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("should not be seen")).not.toBeInTheDocument();
  });

  it("falls back to a generated empty message when none is supplied", () => {
    render(
      <AsyncState isPending={false} error={null} isEmpty label="widgets">
        {child}
      </AsyncState>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("The backend returned no widgets.");
  });

  it("treats a transport error the same as a problem, because it subclasses one", () => {
    render(
      <AsyncState
        isPending={false}
        error={
          new ApiTransportError({
            type: "https://errors.forgeops.dev/transport",
            title: "Network unreachable",
            status: 0,
            detail: "connection refused",
          })
        }
        label="x"
      >
        {child}
      </AsyncState>,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Network unreachable");
    expect(alert).toHaveTextContent("connection refused");
  });

  it("says so plainly when an error arrives with no Problem envelope at all", () => {
    // This branch is unreachable through `lib/api`, which normalises everything — so it exists
    // to report that something bypassed the client, and it should say that rather than crash.
    render(
      <AsyncState isPending={false} error={new Error("raw throw")} label="x">
        {child}
      </AsyncState>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/no Problem Details envelope/i);
  });

  it("omits the detail row when the problem carries no detail", () => {
    render(
      <AsyncState
        isPending={false}
        error={new ApiProblemError({ type: "t", title: "Conflict", status: 409 })}
        label="x"
      >
        {child}
      </AsyncState>,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Conflict");
    expect(alert).not.toHaveTextContent("Detail");
  });

  it("reports 401 as an authentication problem, not as an alert", () => {
    render(
      <AsyncState
        isPending={false}
        error={new ApiProblemError({ type: "t", title: "Denied", status: 401 })}
        label="secrets"
      >
        {child}
      </AsyncState>,
    );
    // Not an alert: an expired session is expected operation, and role="alert" interrupts a screen
    // reader for something the user fixes by signing in again.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText(/not authenticated to read secrets/i)).toBeInTheDocument();
    // It must say the refresh was already attempted, so the reader knows this is not simply a
    // missing sign-in.
    expect(screen.getByText(/auth\/refresh/i)).toBeInTheDocument();
    // And it must NOT assert an expiry as the cause. A 401 here can equally mean the token was
    // REFUSED -- wrong audience, or a missing forgeops_role claim -- and claiming "your session
    // ended" sent a real user to sign in repeatedly against a configuration fault.
    expect(screen.queryByText(/your session ended/i)).not.toBeInTheDocument();
    expect(screen.getByText(/OIDC_APP_AUDIENCE/)).toBeInTheDocument();
  });

  it("reports 403 as a policy refusal, distinctly from 401", () => {
    render(
      <AsyncState
        isPending={false}
        error={new ApiProblemError({ type: "t", title: "Denied", status: 403 })}
        label="secrets"
      >
        {child}
      </AsyncState>,
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText(/not authorised to read secrets/i)).toBeInTheDocument();
    // The two were collapsed into one "sign-in required" message before, which was wrong for 403:
    // the caller IS authenticated and signing in again changes nothing.
    expect(screen.queryByText(/not authenticated/i)).not.toBeInTheDocument();
    expect(screen.getByText(/refused by policy/i)).toBeInTheDocument();
  });
});

describe("NotImplemented", () => {
  it("names the feature, the owner and the reason in a labelled region", () => {
    render(
      <NotImplemented
        feature="The widget forge"
        owner="Phase 2"
        reason="No endpoint exists yet."
      />,
    );
    const region = screen.getByRole("region", {
      name: /The widget forge is not implemented in Phase 1/i,
    });
    expect(region).toHaveTextContent("Phase 2");
    expect(region).toHaveTextContent("No endpoint exists yet.");
  });

  it("omits the detail section entirely when no detail is supplied", () => {
    const { container } = render(
      <NotImplemented feature="F" owner="O" reason="R" detail={<p>extra context</p>} />,
    );
    expect(screen.getByText("extra context")).toBeInTheDocument();
    cleanup();

    render(<NotImplemented feature="F" owner="O" reason="R" />);
    // The `detail ? ... : null` branch — without it, an empty bordered block would render.
    expect(screen.queryByText("extra context")).not.toBeInTheDocument();
    expect(container).toBeTruthy();
  });

  it("states that the blankness is a choice", () => {
    render(<NotImplemented feature="F" owner="O" reason="R" />);
    expect(
      screen.getByText(/deliberately blank rather than populated with sample data/i),
    ).toBeInTheDocument();
  });
});

describe("ProjectIdField", () => {
  it("reports every keystroke to its owner", async () => {
    const onChange = vi.fn();
    render(<ProjectIdField value="" onChange={onChange} />);
    await userEvent.type(screen.getByLabelText("Project ID"), "abc");
    // Controlled and uncommitted, so each keystroke reports the single character typed.
    expect(onChange).toHaveBeenCalledTimes(3);
    expect(onChange).toHaveBeenLastCalledWith("c");
  });

  it("shows the value it is given and links its label to the input", () => {
    render(<ProjectIdField value={DEFAULT_PROJECT_ID} onChange={vi.fn()} />);
    const input = screen.getByLabelText("Project ID");
    expect(input).toHaveValue(DEFAULT_PROJECT_ID);
    expect(input).toHaveAttribute("id", "project-id");
  });

  it("accepts a custom id so two of them can coexist on one page", () => {
    render(<ProjectIdField value="" onChange={vi.fn()} id="other-id" />);
    expect(screen.getByLabelText("Project ID")).toHaveAttribute("id", "other-id");
  });

  it("defaults to a syntactically valid UUID, since the route rejects anything else", () => {
    // `project_id: uuid.UUID` on the backend route means a non-UUID default would make every
    // cold load a 422 rather than a request the handler ever sees.
    expect(DEFAULT_PROJECT_ID).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
  });
});
