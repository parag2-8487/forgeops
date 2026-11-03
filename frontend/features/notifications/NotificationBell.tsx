// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

/**
 * §2.6's bell and preferences.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, queryKeys } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export type DeliveryOutcome = { delivered: boolean; detail: string };

export type Notification = {
  id: string;
  kind: string;
  subject: string;
  body: string;
  resource_kind: string | null;
  resource_id: string | null;
  delivery: Record<string, DeliveryOutcome>;
  read_at: string | null;
  created_at: string | null;
};

type NotificationList = { notifications: Notification[]; unread: number; kinds: string[] };

export type Preference = {
  channel: string;
  kind: string;
  enabled: boolean;
  /** Whether a webhook or address is set. Never the value. */
  target_configured: boolean;
};

type PreferenceList = { preferences: Preference[]; channels: string[]; kinds: string[] };

/** A sentence describing where one notification actually got to. */
export function deliverySentence(delivery: Record<string, DeliveryOutcome>): string {
  const entries = Object.entries(delivery);
  if (entries.length === 0) return "no channel was configured, so this was only recorded here";
  const delivered = entries.filter(([, outcome]) => outcome.delivered).map(([channel]) => channel);
  const failed = entries.filter(([, outcome]) => !outcome.delivered).map(([channel]) => channel);
  if (failed.length === 0) return `delivered to ${delivered.join(", ")}`;
  if (delivered.length === 0) return `delivered nowhere; ${failed.join(", ")} failed`;
  return `delivered to ${delivered.join(", ")}; ${failed.join(", ")} failed`;
}

export function NotificationBell({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);

  const list = useQuery<NotificationList>({
    queryKey: queryKeys.notifications.list(projectId),
    queryFn: () => api.get<NotificationList>(`/projects/${projectId}/notifications`),
  });

  const markRead = useMutation<unknown, Error, string>({
    mutationFn: (id) => api.post(`/projects/${projectId}/notifications/${id}/read`, {}),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.notifications.all });
    },
  });

  const unread = list.data?.unread ?? 0;

  return (
    <div data-testid="notification-bell" className="space-y-3">
      <Button
        type="button"
        variant="outline"
        size="sm"
        data-testid="bell-toggle"
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2"
      >
        <span>Notifications</span>
        {list.isLoading ? (
          <span
            data-testid="bell-count-unknown"
            className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
          >
            checking
          </span>
        ) : list.isError ? (
          <span
            data-testid="bell-count-error"
            className="rounded-full bg-destructive/10 px-2 py-0.5 text-xs text-destructive"
          >
            error
          </span>
        ) : (
          <span
            data-testid="bell-count"
            className="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-semibold text-primary"
          >
            {unread} unread
          </span>
        )}
      </Button>

      {open && (
        <Card data-testid="bell-dropdown" className="border border-border shadow-md">
          <CardHeader className="py-3 px-4 border-b border-border">
            <CardTitle className="text-sm font-semibold">Project Notifications</CardTitle>
          </CardHeader>
          <CardContent className="p-4">
            {list.isError ? (
              <p data-testid="bell-error" role="alert" className="text-sm text-destructive">
                The notifications could not be read. This is not the same as there being none.
              </p>
            ) : (list.data?.notifications.length ?? 0) === 0 ? (
              <p data-testid="bell-empty" className="text-sm text-muted-foreground">
                Nothing has been raised for this project.
              </p>
            ) : (
              <ul data-testid="bell-list" className="divide-y divide-border space-y-3">
                {list.data?.notifications.map((notification) => (
                  <li
                    key={notification.id}
                    data-testid={`bell-item-${notification.id}`}
                    className="pt-3 first:pt-0 space-y-1.5"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <strong className="text-sm font-medium">{notification.subject}</strong>
                      {notification.read_at === null && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          data-testid={`bell-read-${notification.id}`}
                          onClick={() => markRead.mutate(notification.id)}
                          className="h-7 text-xs text-primary"
                        >
                          mark read
                        </Button>
                      )}
                    </div>
                    <p className="text-xs text-muted-foreground">{notification.body}</p>
                    <div className="text-xs text-muted-foreground font-mono">
                      <span data-testid={`bell-delivery-${notification.id}`}>
                        {deliverySentence(notification.delivery)}
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

export function NotificationPreferences({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [target, setTarget] = useState("");
  const [channel, setChannel] = useState("slack");
  const [kind, setKind] = useState("deploy_failed");
  const [message, setMessage] = useState<string | null>(null);

  const preferences = useQuery<PreferenceList>({
    queryKey: queryKeys.notifications.preferences(projectId),
    queryFn: () => api.get<PreferenceList>(`/projects/${projectId}/notifications/preferences`),
  });

  const save = useMutation<unknown, Error, Preference & { target: string | null }>({
    mutationFn: (body) =>
      api.put(`/projects/${projectId}/notifications/preferences`, {
        channel: body.channel,
        kind: body.kind,
        enabled: body.enabled,
        target: body.target,
      }),
    onSuccess: () => {
      setMessage("saved");
      setTarget("");
      void queryClient.invalidateQueries({ queryKey: queryKeys.notifications.all });
    },
  });

  return (
    <Card
      aria-label="Notification preferences"
      data-testid="notification-preferences"
      className="border border-border"
    >
      <CardHeader>
        <CardTitle className="text-base font-semibold">Notification channels</CardTitle>
        <CardDescription data-testid="preferences-credential-note">
          A webhook URL is a credential, so it is never shown again after it is saved — only whether
          one is set.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <form
          className="space-y-4 rounded-lg border border-border bg-muted/20 p-4"
          onSubmit={(event) => event.preventDefault()}
        >
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label htmlFor="pref-channel" className="block text-sm font-medium">
                Channel
              </label>
              <select
                id="pref-channel"
                data-testid="pref-channel"
                value={channel}
                onChange={(event) => setChannel(event.target.value)}
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {(preferences.data?.channels ?? ["in_app", "slack", "discord", "email"]).map(
                  (name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ),
                )}
              </select>
            </div>

            <div className="space-y-1.5">
              <label htmlFor="pref-kind" className="block text-sm font-medium">
                When
              </label>
              <select
                id="pref-kind"
                data-testid="pref-kind"
                value={kind}
                onChange={(event) => setKind(event.target.value)}
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {(preferences.data?.kinds ?? []).map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {channel !== "in_app" && (
            <div className="space-y-1.5">
              <label htmlFor="pref-target" className="block text-sm font-medium">
                Webhook URL or address
              </label>
              <input
                id="pref-target"
                data-testid="pref-target"
                value={target}
                onChange={(event) => setTarget(event.target.value)}
                placeholder="https://hooks.slack.com/services/..."
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
          )}

          <div className="flex flex-wrap items-center gap-3 pt-1">
            <Button
              type="button"
              data-testid="pref-save"
              disabled={save.isPending || (channel !== "in_app" && target.trim() === "")}
              onClick={() =>
                save.mutate({
                  channel,
                  kind,
                  enabled: true,
                  target_configured: true,
                  target: target || null,
                })
              }
              className="px-4 py-2 font-medium"
            >
              {save.isPending ? "Saving…" : "enable"}
            </Button>
            {channel !== "in_app" && target.trim() === "" && (
              <span
                data-testid="pref-target-required"
                className="text-xs text-amber-600 dark:text-amber-400"
              >
                this channel needs somewhere to deliver
              </span>
            )}
            {message && (
              <span
                data-testid="pref-message"
                className="rounded bg-emerald-500/10 px-2 py-1 text-xs font-medium text-emerald-600 dark:text-emerald-400"
              >
                {message}
              </span>
            )}
          </div>
        </form>

        <div className="space-y-3">
          <h4 className="text-xs uppercase tracking-wide text-muted-foreground font-semibold">
            Configured Channels
          </h4>
          {preferences.data && (
            <ul data-testid="pref-list" className="space-y-2">
              {preferences.data.preferences.length === 0 ? (
                <li className="rounded-md border border-dashed border-border p-4 text-xs text-muted-foreground text-center">
                  No channels are configured, so notifications are recorded here only.
                </li>
              ) : (
                preferences.data.preferences.map((preference) => (
                  <li
                    key={`${preference.channel}-${preference.kind}`}
                    className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-card p-3 text-sm shadow-sm"
                  >
                    <div className="flex items-center gap-2">
                      <strong className="font-mono">{preference.channel}</strong>
                      <span className="text-muted-foreground">on {preference.kind}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <Badge variant={preference.enabled ? "success" : "outline"}>
                        {preference.enabled ? "enabled" : "disabled"}
                      </Badge>
                      <span
                        data-testid={`pref-target-${preference.channel}-${preference.kind}`}
                        className="rounded bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground"
                      >
                        {preference.target_configured ? "target configured" : "no target set"}
                      </span>
                    </div>
                  </li>
                ))
              )}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
