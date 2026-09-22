"use client";

/**
 * §2.6's bell and preferences.
 *
 * The bell shows PER-CHANNEL delivery, which is the point: a notification that reached the bell and was
 * rejected by Slack has to say so, or an operator believes their team was told. "Nothing was delivered
 * anywhere" is rendered as its own line rather than as an absence of badges.
 *
 * Preferences never display a target. A webhook URL is a credential — anybody holding it can post into the
 * channel — so the server returns `target_configured` and this screen shows "configured" or "not set".
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, queryKeys } from "@/lib/api";

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
    <div data-testid="notification-bell">
      <button type="button" data-testid="bell-toggle" onClick={() => setOpen(!open)}>
        Notifications
        {/* THE COUNT IS ABSENT, NOT ZERO, WHILE LOADING: a badge reading 0 on a project with unread
            notifications is worse than no badge. */}
        {list.isLoading ? (
          <span data-testid="bell-count-unknown"> (checking)</span>
        ) : list.isError ? (
          <span data-testid="bell-count-error"> (could not be read)</span>
        ) : (
          <span data-testid="bell-count"> ({unread} unread)</span>
        )}
      </button>

      {open && (
        <div data-testid="bell-dropdown">
          {list.isError ? (
            <p data-testid="bell-error" role="alert">
              The notifications could not be read. This is not the same as there being none.
            </p>
          ) : (list.data?.notifications.length ?? 0) === 0 ? (
            <p data-testid="bell-empty">Nothing has been raised for this project.</p>
          ) : (
            <ul data-testid="bell-list">
              {list.data?.notifications.map((notification) => (
                <li key={notification.id} data-testid={`bell-item-${notification.id}`}>
                  <strong>{notification.subject}</strong>
                  <p>{notification.body}</p>
                  <span data-testid={`bell-delivery-${notification.id}`}>
                    {deliverySentence(notification.delivery)}
                  </span>
                  {notification.read_at === null && (
                    <button
                      type="button"
                      data-testid={`bell-read-${notification.id}`}
                      onClick={() => markRead.mutate(notification.id)}
                    >
                      mark read
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
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
    <section aria-label="Notification preferences" data-testid="notification-preferences">
      <p data-testid="preferences-credential-note">
        A webhook URL is a credential, so it is never shown again after it is saved — only whether
        one is set.
      </p>

      <label htmlFor="pref-channel">Channel</label>
      <select
        id="pref-channel"
        data-testid="pref-channel"
        value={channel}
        onChange={(event) => setChannel(event.target.value)}
      >
        {(preferences.data?.channels ?? ["in_app", "slack", "discord", "email"]).map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>

      <label htmlFor="pref-kind">When</label>
      <select
        id="pref-kind"
        data-testid="pref-kind"
        value={kind}
        onChange={(event) => setKind(event.target.value)}
      >
        {(preferences.data?.kinds ?? []).map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>

      {channel !== "in_app" && (
        <>
          <label htmlFor="pref-target">Webhook URL or address</label>
          <input
            id="pref-target"
            data-testid="pref-target"
            value={target}
            onChange={(event) => setTarget(event.target.value)}
          />
        </>
      )}

      <button
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
      >
        enable
      </button>
      {channel !== "in_app" && target.trim() === "" && (
        <span data-testid="pref-target-required">
          {/* Stated before the save rather than after a refusal: an enabled channel with nowhere to
              deliver is a setting that silently does nothing. */}
          this channel needs somewhere to deliver
        </span>
      )}
      {message && <span data-testid="pref-message">{message}</span>}

      {preferences.data && (
        <ul data-testid="pref-list">
          {preferences.data.preferences.length === 0 ? (
            <li>No channels are configured, so notifications are recorded here only.</li>
          ) : (
            preferences.data.preferences.map((preference) => (
              <li key={`${preference.channel}-${preference.kind}`}>
                {preference.channel} on {preference.kind}:{" "}
                {preference.enabled ? "enabled" : "disabled"},{" "}
                <span data-testid={`pref-target-${preference.channel}-${preference.kind}`}>
                  {preference.target_configured ? "target configured" : "no target set"}
                </span>
              </li>
            ))
          )}
        </ul>
      )}
    </section>
  );
}
