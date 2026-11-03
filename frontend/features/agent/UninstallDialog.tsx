"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Loader2, Trash2 } from "lucide-react";
import { api, queryKeys } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { CommandBlock } from "./CommandBlock";
import { renderCommand, type Platform, type Shell } from "./commands";

interface AgentUninstallResult {
  stopped_devices: string[];
  removed_devices: string[];
  cleaned_entries: string[];
  skipped: Array<{ device_id: string; reason: string }>;
  remaining_count: number;
  message: string;
  success: boolean;
}

export interface UninstallDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  platform: Platform;
  shell: Shell;
  projectId?: string;
}

export function UninstallDialog({
  open,
  onOpenChange,
  platform,
  shell,
  projectId,
}: UninstallDialogProps) {
  const queryClient = useQueryClient();
  const [wipeCredentials, setWipeCredentials] = useState(false);
  const [result, setResult] = useState<AgentUninstallResult | null>(null);

  const uninstallCommand = renderCommand({
    verb: "uninstall",
    platform,
    shell,
    location: { kind: "on-path" },
    flags: {
      force: true,
      ...(wipeCredentials ? { "wipe-credentials": true } : {}),
    },
  });

  const mutation = useMutation({
    mutationFn: () =>
      api.post<AgentUninstallResult>("/agents/uninstall", {
        project_id: projectId || undefined,
        dry_run: false,
        force: true,
        wipe_credentials: wipeCredentials,
      }),
    onSuccess: (data) => {
      setResult(data);
      void queryClient.invalidateQueries({ queryKey: queryKeys.devices.all });
      void queryClient.invalidateQueries({ queryKey: ["agents", "connection-info"] });
    },
  });

  const handleClose = () => {
    setResult(null);
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="uninstall-dialog"
        className="max-h-[90vh] overflow-y-auto sm:max-w-xl"
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-destructive">
            <Trash2 className="h-5 w-5" />
            Uninstall All ForgeOps Agents
          </DialogTitle>
          <DialogDescription className="text-xs">
            Locate and remove all verified ForgeOps agent installations belonging to your user
            account across known locations on this machine.
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <div className="space-y-4 py-2" data-testid="uninstall-results">
            <div className="flex items-center gap-2 rounded-md border border-green-500/30 bg-green-500/10 p-3 text-sm text-green-700 dark:text-green-300">
              <CheckCircle2 className="h-5 w-5 shrink-0" />
              <div>
                <p className="font-semibold">{result.message}</p>
                <p className="text-xs text-muted-foreground">
                  Status: {result.success ? "Removed successfully" : "Partially removed"}
                </p>
              </div>
            </div>

            <div className="space-y-2 text-xs">
              <p className="font-medium">Execution Summary:</p>
              <ul className="list-inside list-disc space-y-1 text-muted-foreground">
                <li>
                  Stopped agent processes / sessions:{" "}
                  <span className="font-mono text-foreground">{result.stopped_devices.length}</span>
                </li>
                <li>
                  Revoked registered devices:{" "}
                  <span className="font-mono text-foreground">{result.removed_devices.length}</span>
                </li>
                {result.cleaned_entries.length > 0 ? (
                  <li>
                    Cleaned credential registrations:{" "}
                    <span className="font-mono text-foreground">
                      {result.cleaned_entries.join(", ")}
                    </span>
                  </li>
                ) : null}
                {result.skipped.length > 0 ? (
                  <li className="text-amber-600 dark:text-amber-400">
                    Skipped / Remaining: {result.skipped.length}
                  </li>
                ) : null}
              </ul>
            </div>

            <div className="rounded-md border border-border bg-muted/40 p-3 text-xs">
              <p className="font-medium text-foreground">Next step:</p>
              <p className="mt-1 text-muted-foreground">
                Download and unpack the latest release from the Download section above, then run the
                install command to ensure the intended agent binary is active on your PATH.
              </p>
            </div>

            <DialogFooter>
              <Button onClick={handleClose} data-testid="uninstall-dismiss-btn">
                Done
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <div className="space-y-4 py-2 text-xs">
            <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-amber-800 dark:text-amber-200">
              <div className="flex items-start gap-2">
                <AlertTriangle className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
                <div className="space-y-1">
                  <p className="font-medium">Scope &amp; Safety Notice</p>
                  <p className="text-muted-foreground">
                    Active ForgeOps agent sessions will be immediately disconnected. Only verified
                    ForgeOps agent binaries (in <code>%LOCALAPPDATA%\Programs\ForgeOps</code>,{" "}
                    <code>~/go/bin</code>, <code>/usr/local/bin</code>, and PATH) will be removed.
                    Your project files, source workspaces, and repository code remain untouched.
                  </p>
                </div>
              </div>
            </div>

            <div className="space-y-2">
              <label className="flex items-center gap-2 cursor-pointer font-medium">
                <input
                  type="checkbox"
                  checked={wipeCredentials}
                  onChange={(e) => setWipeCredentials(e.target.checked)}
                  data-testid="wipe-credentials-checkbox"
                  className="rounded border-border"
                />
                <span>Also wipe local pairing credentials (%APPDATA%\forgeops)</span>
              </label>
              <p className="text-muted-foreground pl-5">
                Leave unchecked if you wish to preserve existing certificates for fast re-pairing.
              </p>
            </div>

            <div className="space-y-2">
              <p className="font-medium">Local CLI Execution Command:</p>
              <CommandBlock
                command={uninstallCommand}
                caption="Run this in your terminal to directly stop running background processes, remove all installed ForgeOps binaries, and clean user PATH entries on this PC."
                testId="local-uninstall-command"
              />
            </div>

            {mutation.isError ? (
              <p className="text-destructive font-medium">
                Error during uninstall: {(mutation.error as Error).message}
              </p>
            ) : null}

            <DialogFooter className="gap-2 sm:gap-0">
              <Button
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={mutation.isPending}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={() => mutation.mutate()}
                disabled={mutation.isPending}
                data-testid="confirm-uninstall-btn"
                className="gap-2"
              >
                {mutation.isPending ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Uninstalling…
                  </>
                ) : (
                  "Uninstall & Revoke Sessions"
                )}
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
