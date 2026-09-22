# SPDX-License-Identifier: FSL-1.1-ALv2
"""§2.6's notifications against the real database.

Four properties. A template rendered without its fields FAILS rather than emitting `{deployment_id}` to
somebody's Slack channel. A configured channel with no adapter is recorded as UNDELIVERED, not skipped —
silence would let an operator believe Slack was told because they enabled it. One channel's failure does not
stop the others. And a preference read never returns the target, because a webhook URL is a credential.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.notifications.service import TEMPLATES, NotificationService

from tests.integration.chokepoint_support import make_fixture

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


class RecordingChannel:
    """A channel that records what it was handed. The transport, not the service."""

    def __init__(self, name: str, *, fails: bool = False) -> None:
        self.name = name
        self.sent: list[tuple[str, str, str]] = []
        self._fails = fails

    async def send(self, *, target: str, subject: str, body: str) -> None:
        if self._fails:
            raise RuntimeError("the webhook returned 500")
        self.sent.append((target, subject, body))


async def _enable(
    session: AsyncSession,
    service: NotificationService,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    channel: str,
    kind: str,
    target: str | None,
) -> None:
    await service.set_preference(
        session,
        user_id=user_id,
        project_id=project_id,
        channel=channel,
        kind=kind,
        enabled=True,
        target=target,
    )


class TestTemplates:
    def test_every_kind_has_a_template(self) -> None:
        from src.notifications.service import NOTIFICATION_KINDS

        for kind in NOTIFICATION_KINDS:
            assert kind in TEMPLATES, f"{kind} has no template, so raising it would fail at runtime"
        assert len(TEMPLATES) == len(NOTIFICATION_KINDS)

    def test_a_missing_field_fails_rather_than_emitting_a_placeholder(self) -> None:
        # THE FAILURE THIS PREVENTS: a Slack message reading "Deployment {deployment_id} to staging".
        with pytest.raises(ValueError, match="deployment_id"):
            TEMPLATES["deploy_completed"].render({"project": "demo", "environment": "staging", "manifest_count": 2})

    def test_a_complete_render_substitutes_everything(self) -> None:
        subject, body = TEMPLATES["deploy_failed"].render(
            {
                "project": "demo",
                "environment": "production",
                "deployment_id": "d-1",
                "status": "degraded",
                "detail": "checkout-api never became ready",
            }
        )
        assert "{" not in subject and "}" not in subject
        assert "{" not in body and "}" not in body
        assert "degraded" in body
        # The wording must keep applied apart from running, which is the whole point of `degraded`.
        assert "workloads became ready" in body


class TestDelivery:
    async def test_each_enabled_channel_is_recorded_and_one_failure_does_not_stop_another(
        self, sessions: Any, redis_client: Any
    ) -> None:
        slack = RecordingChannel("slack")
        discord = RecordingChannel("discord", fails=True)
        service = NotificationService(channels={"slack": slack, "discord": discord})
        async with sessions() as session:
            fixture = await make_fixture(session)
            user_id = uuid.uuid4()
            for channel, target in (
                ("slack", "https://hooks.slack.test/abc"),
                ("discord", "https://discord.test/hook"),
                ("in_app", None),
            ):
                await _enable(
                    session,
                    service,
                    user_id=user_id,
                    project_id=fixture.project_id,
                    channel=channel,
                    kind="deploy_failed",
                    target=target,
                )
            record = await service.raise_notification(
                session,
                project_id=fixture.project_id,
                tenant_id=None,
                kind="deploy_failed",
                values={
                    "project": "demo",
                    "environment": "production",
                    "deployment_id": "d-1",
                    "status": "degraded",
                    "detail": "checkout-api never became ready",
                },
                resource_kind="deployment",
                resource_id="d-1",
            )

        # Slack got it; Discord failed and SAYS SO; in_app is recorded so the map is complete.
        assert len(slack.sent) == 1
        assert record.delivery["slack"]["delivered"] is True
        assert record.delivery["discord"]["delivered"] is False
        assert "500" in record.delivery["discord"]["detail"]
        assert record.delivery["in_app"]["delivered"] is True
        # One channel's failure did not stop the other, which is why each is attempted separately.
        assert len(record.delivery) == 3

    async def test_a_channel_with_no_adapter_is_undelivered_rather_than_silent(
        self, sessions: Any, redis_client: Any
    ) -> None:
        # No adapters composed at all: an operator who enabled Slack must not be told it went out.
        service = NotificationService(channels={})
        async with sessions() as session:
            fixture = await make_fixture(session)
            await _enable(
                session,
                service,
                user_id=uuid.uuid4(),
                project_id=fixture.project_id,
                channel="slack",
                kind="policy_violated",
                target="https://hooks.slack.test/abc",
            )
            record = await service.raise_notification(
                session,
                project_id=fixture.project_id,
                tenant_id=None,
                kind="policy_violated",
                values={"project": "demo", "change_set_id": "cs-1", "detail": "the bundle denied it"},
            )
        assert record.delivery["slack"]["delivered"] is False
        assert "no adapter" in record.delivery["slack"]["detail"]

    async def test_the_row_exists_even_when_every_channel_fails(self, sessions: Any, redis_client: Any) -> None:
        """ "Was anybody told?" has to be answerable, so the row is written before delivery is attempted."""
        service = NotificationService(channels={"slack": RecordingChannel("slack", fails=True)})
        async with sessions() as session:
            fixture = await make_fixture(session)
            await _enable(
                session,
                service,
                user_id=uuid.uuid4(),
                project_id=fixture.project_id,
                channel="slack",
                kind="approval_required",
                target="https://hooks.slack.test/abc",
            )
            await service.raise_notification(
                session,
                project_id=fixture.project_id,
                tenant_id=None,
                kind="approval_required",
                values={"project": "demo", "change_set_id": "cs-2", "detail": "production needs a human"},
            )
            stored = (
                await session.execute(
                    text("SELECT count(*) FROM notifications WHERE project_id = :p"),
                    {"p": fixture.project_id},
                )
            ).scalar_one()
        assert stored == 1

    async def test_an_unknown_kind_is_refused(self, sessions: Any, redis_client: Any) -> None:
        service = NotificationService(channels={})
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(ValueError, match="not a notification kind"):
                await service.raise_notification(
                    session,
                    project_id=fixture.project_id,
                    tenant_id=None,
                    kind="everything_is_fine",
                    values={"project": "demo"},
                )


class TestPreferences:
    async def test_an_enabled_channel_needs_a_target(self, sessions: Any, redis_client: Any) -> None:
        service = NotificationService(channels={})
        async with sessions() as session:
            fixture = await make_fixture(session)
            # Refused in the service as well as by the CHECK: a user who enabled Slack with no webhook
            # believes they are covered, and nothing would ever arrive.
            with pytest.raises(ValueError, match="needs a target"):
                await service.set_preference(
                    session,
                    user_id=uuid.uuid4(),
                    project_id=fixture.project_id,
                    channel="slack",
                    kind="deploy_failed",
                    enabled=True,
                    target=None,
                )

    async def test_in_app_needs_no_target(self, sessions: Any, redis_client: Any) -> None:
        service = NotificationService(channels={})
        async with sessions() as session:
            fixture = await make_fixture(session)
            await service.set_preference(
                session,
                user_id=uuid.uuid4(),
                project_id=fixture.project_id,
                channel="in_app",
                kind="deploy_failed",
                enabled=True,
                target=None,
            )

    async def test_the_target_is_never_returned(self, sessions: Any, redis_client: Any) -> None:
        """A webhook URL is a credential: anybody holding it can post into the channel."""
        service = NotificationService(channels={})
        secret_hook = "https://hooks.slack.test/T000/B000/" + "z" * 24
        async with sessions() as session:
            fixture = await make_fixture(session)
            user_id = uuid.uuid4()
            await _enable(
                session,
                service,
                user_id=user_id,
                project_id=fixture.project_id,
                channel="slack",
                kind="deploy_completed",
                target=secret_hook,
            )
            preferences = await service.preferences(session, user_id=user_id, project_id=fixture.project_id)

        assert len(preferences) == 1
        assert preferences[0]["target_configured"] is True
        # Searched across the whole payload rather than for an absent key, so a field added later that
        # happened to carry it would fail this too.
        assert secret_hook not in str(preferences)

    async def test_setting_the_same_preference_twice_updates_rather_than_duplicating(
        self, sessions: Any, redis_client: Any
    ) -> None:
        service = NotificationService(channels={})
        async with sessions() as session:
            fixture = await make_fixture(session)
            user_id = uuid.uuid4()
            for target in ("https://hooks.slack.test/first", "https://hooks.slack.test/second"):
                await _enable(
                    session,
                    service,
                    user_id=user_id,
                    project_id=fixture.project_id,
                    channel="slack",
                    kind="deploy_completed",
                    target=target,
                )
            preferences = await service.preferences(session, user_id=user_id, project_id=fixture.project_id)
        # One row, not two: a duplicate would make delivery send twice and a read choose arbitrarily.
        assert len(preferences) == 1
