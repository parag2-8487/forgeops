# SPDX-License-Identifier: FSL-1.1-ALv2
"""Notifications: templates, per-user preferences, and delivery to real channels. §2.6.

WHY NOT NOVU. §2.6's first box names Novu, and that box is deliberately left open in `phases.md` rather than
ticked by this module: Novu is a hosted or self-hosted service needing an API key this deployment does not
have, and wiring an unreachable client would put a placeholder on a runtime path. What is here instead is the
same shape behind a `Channel` Protocol — so a Novu adapter is a new class, not a rewrite — with three real
channels that need nothing but a webhook URL or an SMTP host.

WHAT IS STORED AND WHY. Every notification is a row, with a per-channel delivery outcome, before and after
the attempt. "Was anybody told?" is a question an incident review asks, and a webhook POST that vanished
cannot answer it. `delivery` is a map rather than a status column because one notification can succeed on
Slack and be rejected by SMTP, and a single column would have to pick one and hide the other.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: The closed set of things worth telling somebody about. Kept identical to revision `0028`'s CHECK.
NOTIFICATION_KINDS: Final[tuple[str, ...]] = (
    "deploy_completed",
    "deploy_failed",
    "policy_violated",
    "approval_required",
    "self_healed",
)

#: The closed set of channels. `in_app` is the bell and needs no target.
NOTIFICATION_CHANNELS: Final[tuple[str, ...]] = ("in_app", "slack", "discord", "email")


@dataclass(frozen=True, slots=True)
class NotificationTemplate:
    """One kind's subject and body, with the fields it requires.

    `required` is declared rather than discovered so a template rendered with a missing field FAILS here
    instead of emitting the literal `{deployment_id}` to an operator's Slack channel — which is the quiet
    version of this codebase's fabrication problem.
    """

    kind: str
    subject: str
    body: str
    required: tuple[str, ...]

    def render(self, values: dict[str, Any]) -> tuple[str, str]:
        missing = [name for name in self.required if not values.get(name)]
        if missing:
            raise ValueError(
                f"the {self.kind} template needs {', '.join(missing)}; rendering without them would send "
                "a message containing an unsubstituted placeholder"
            )
        return self.subject.format(**values), self.body.format(**values)


#: §2.6's templates. Deliberately plain text: a Slack webhook, a Discord webhook and an SMTP body have three
#: different markup dialects, and the one thing all three render identically is prose.
TEMPLATES: Final[dict[str, NotificationTemplate]] = {
    "deploy_completed": NotificationTemplate(
        kind="deploy_completed",
        subject="{project}: deployment to {environment} converged",
        body=(
            "Deployment {deployment_id} to {environment} applied {manifest_count} manifest(s) and every "
            "workload became ready."
        ),
        required=("project", "environment", "deployment_id", "manifest_count"),
    ),
    "deploy_failed": NotificationTemplate(
        kind="deploy_failed",
        subject="{project}: deployment to {environment} did not converge",
        body=(
            "Deployment {deployment_id} to {environment} finished as {status}. The objects may be in the "
            "cluster; what did not happen is that the workloads became ready. Detail: {detail}"
        ),
        required=("project", "environment", "deployment_id", "status", "detail"),
    ),
    "policy_violated": NotificationTemplate(
        kind="policy_violated",
        subject="{project}: a change was refused by policy",
        body="Change set {change_set_id} was refused: {detail}",
        required=("project", "change_set_id", "detail"),
    ),
    "approval_required": NotificationTemplate(
        kind="approval_required",
        subject="{project}: a change is waiting for approval",
        body="Change set {change_set_id} needs a human before it can proceed: {detail}",
        required=("project", "change_set_id", "detail"),
    ),
    "self_healed": NotificationTemplate(
        kind="self_healed",
        subject="{project}: an automatic repair ran",
        body="{detail}",
        required=("project", "detail"),
    ),
}


@runtime_checkable
class Channel(Protocol):
    """One delivery mechanism.

    A Protocol so a Novu adapter, or any other, is a new class rather than a change here — and so the
    service can be tested without reaching the network.
    """

    name: str

    async def send(self, *, target: str, subject: str, body: str) -> None: ...


@dataclass(slots=True)
class DeliveryOutcome:
    """What became of one channel's attempt."""

    channel: str
    delivered: bool
    detail: str


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    id: uuid.UUID
    project_id: uuid.UUID
    kind: str
    subject: str
    body: str
    resource_kind: str | None
    resource_id: str | None
    delivery: dict[str, Any]
    read_at: str | None
    created_at: str | None


@dataclass(slots=True)
class NotificationService:
    """Raises notifications, renders them, and delivers to each enabled channel.

    The row is written BEFORE delivery is attempted, for the reason the deployment record is: a notification
    whose webhook failed still has to exist, or the only trace of an attempt is a log line nobody reads.
    """

    channels: dict[str, Channel] = field(default_factory=dict)

    async def raise_notification(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        kind: str,
        values: dict[str, Any],
        resource_kind: str | None = None,
        resource_id: str | None = None,
    ) -> NotificationRecord:
        template = TEMPLATES.get(kind)
        if template is None:
            raise ValueError(f"{kind!r} is not a notification kind; one of {', '.join(NOTIFICATION_KINDS)}")
        subject, body = template.render(values)

        notification_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO notifications (id, project_id, tenant_id, kind, subject, body, "
                "resource_kind, resource_id) "
                "VALUES (:id, :project, :tenant, :kind, :subject, :body, :rkind, :rid)"
            ),
            {
                "id": notification_id,
                "project": project_id,
                "tenant": tenant_id,
                "kind": kind,
                "subject": subject,
                "body": body,
                "rkind": resource_kind,
                "rid": resource_id,
            },
        )

        outcomes = await self._deliver(session, project_id=project_id, kind=kind, subject=subject, body=body)
        if outcomes:
            await session.execute(
                text("UPDATE notifications SET delivery = CAST(:delivery AS jsonb) WHERE id = :id"),
                {
                    "delivery": _as_json({outcome.channel: _outcome_json(outcome) for outcome in outcomes}),
                    "id": notification_id,
                },
            )
        return await self.read(session, notification_id=notification_id)

    async def _deliver(
        self, session: AsyncSession, *, project_id: uuid.UUID, kind: str, subject: str, body: str
    ) -> list[DeliveryOutcome]:
        rows = await session.execute(
            text(
                "SELECT channel, target FROM notification_preferences "
                "WHERE project_id = :project AND kind = :kind AND enabled IS TRUE"
            ),
            {"project": project_id, "kind": kind},
        )
        outcomes: list[DeliveryOutcome] = []
        for row in rows.mappings():
            channel_name = str(row["channel"])
            if channel_name == "in_app":
                # The bell reads the row itself, so there is nothing to send. Recorded as delivered so the
                # map is complete rather than silently missing a channel somebody enabled.
                outcomes.append(DeliveryOutcome("in_app", True, "the row is the delivery"))
                continue
            channel = self.channels.get(channel_name)
            if channel is None:
                # A CONFIGURED CHANNEL WITH NO ADAPTER IS RECORDED AS UNDELIVERED, not skipped. Silence here
                # would let an operator believe Slack was notified because they enabled it.
                outcomes.append(DeliveryOutcome(channel_name, False, "no adapter for this channel is composed"))
                continue
            try:
                await channel.send(target=str(row["target"] or ""), subject=subject, body=body)
            except Exception as error:  # noqa: BLE001 - one channel's failure must not stop the others
                outcomes.append(DeliveryOutcome(channel_name, False, f"{type(error).__name__}: {error}"))
                continue
            outcomes.append(DeliveryOutcome(channel_name, True, "accepted"))
        return outcomes

    async def read(self, session: AsyncSession, *, notification_id: uuid.UUID) -> NotificationRecord:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT id, project_id, kind, subject, body, resource_kind, resource_id, delivery, "
                        "read_at, created_at FROM notifications WHERE id = :id"
                    ),
                    {"id": notification_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise LookupError(f"no notification {notification_id}")
        return _as_record(row)

    async def list_for_project(
        self, session: AsyncSession, *, project_id: uuid.UUID, limit: int = 50
    ) -> list[NotificationRecord]:
        rows = await session.execute(
            text(
                "SELECT id, project_id, kind, subject, body, resource_kind, resource_id, delivery, "
                "read_at, created_at FROM notifications WHERE project_id = :project "
                "ORDER BY created_at DESC LIMIT :limit"
            ),
            {"project": project_id, "limit": limit},
        )
        return [_as_record(row) for row in rows.mappings()]

    async def mark_read(self, session: AsyncSession, *, notification_id: uuid.UUID) -> NotificationRecord:
        await session.execute(
            text("UPDATE notifications SET read_at = :now WHERE id = :id AND read_at IS NULL"),
            {"now": datetime.now(UTC), "id": notification_id},
        )
        return await self.read(session, notification_id=notification_id)

    async def set_preference(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        channel: str,
        kind: str,
        enabled: bool,
        target: str | None,
    ) -> None:
        if channel not in NOTIFICATION_CHANNELS:
            raise ValueError(f"{channel!r} is not a channel; one of {', '.join(NOTIFICATION_CHANNELS)}")
        if kind not in NOTIFICATION_KINDS:
            raise ValueError(f"{kind!r} is not a notification kind")
        if enabled and channel != "in_app" and not (target or "").strip():
            # Refused here as well as by the CHECK: an enabled channel with nowhere to deliver is a setting
            # that silently does nothing, and a user who set it believes they are covered.
            raise ValueError(f"the {channel} channel needs a target (a webhook URL or an address) when it is enabled")
        await session.execute(
            text(
                "INSERT INTO notification_preferences (id, user_id, project_id, channel, kind, enabled, target) "
                "VALUES (:id, :user, :project, :channel, :kind, :enabled, :target) "
                "ON CONFLICT (user_id, project_id, channel, kind) "
                "DO UPDATE SET enabled = :enabled, target = :target"
            ),
            {
                "id": uuid.uuid4(),
                "user": user_id,
                "project": project_id,
                "channel": channel,
                "kind": kind,
                "enabled": enabled,
                "target": target,
            },
        )

    async def preferences(
        self, session: AsyncSession, *, user_id: uuid.UUID, project_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        rows = await session.execute(
            text(
                "SELECT channel, kind, enabled, target FROM notification_preferences "
                "WHERE user_id = :user AND project_id = :project ORDER BY channel, kind"
            ),
            {"user": user_id, "project": project_id},
        )
        return [
            {
                "channel": row["channel"],
                "kind": row["kind"],
                "enabled": bool(row["enabled"]),
                # THE TARGET IS NEVER RETURNED. A webhook URL is a credential — anybody holding it can post
                # into the channel — so the read reports whether one is set, not what it is.
                "target_configured": bool(row["target"]),
            }
            for row in rows.mappings()
        ]


def _outcome_json(outcome: DeliveryOutcome) -> dict[str, Any]:
    return {"delivered": outcome.delivered, "detail": outcome.detail}


def _as_json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))


def _as_record(row: Any) -> NotificationRecord:
    return NotificationRecord(
        id=row["id"],
        project_id=row["project_id"],
        kind=str(row["kind"]),
        subject=str(row["subject"]),
        body=str(row["body"]),
        resource_kind=row["resource_kind"],
        resource_id=row["resource_id"],
        delivery=dict(row["delivery"] or {}),
        read_at=row["read_at"].isoformat() if row["read_at"] else None,
        created_at=row["created_at"].isoformat() if row["created_at"] else None,
    )
