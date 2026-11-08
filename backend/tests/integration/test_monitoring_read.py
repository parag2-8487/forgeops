"""The monitoring read route -- Phase 2 §2.10.

THE CENTRAL CLAIM THIS FILE EXISTS TO PROVE: a caller cannot reach a series outside its tenant, and cannot
reach PromQL at all. Those are tested as separate properties, because they fail in different ways -- the first
by a matcher that can be escaped, the second by a field that forwards a string.

The tenant-escape tests run against a REAL Prometheus holding two tenants' series. A test that only inspected
the rendered query string would prove the string looked right, not that the store refused to return the other
tenant's data, and the difference is the entire security property. Where Prometheus is not running the tests
that need it are skipped as a PLATFORM skip with that reason; the structural tests below need no store and
always run.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.auth.dependencies import require_principal
from src.auth.principal import Principal, UserRole
from src.core.errors import install_problem_handlers
from src.monitoring.queries import (
    CATALOGUE,
    TENANT_SLOT,
    CatalogueEntry,
    CatalogueError,
)
from src.monitoring.reader import MetricsReader
from src.monitoring.routes import router

PROM_URL = os.environ.get("FORGEOPS_TEST_PROMETHEUS_URL", "")


def _app(*, principal: Principal, reader: MetricsReader) -> FastAPI:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(router)
    app.state.metrics_reader = reader
    app.dependency_overrides[require_principal] = lambda: principal
    return app


def _principal(*, role: UserRole = UserRole.DEVELOPER, tenant: uuid.UUID | None = None) -> Principal:
    return Principal.for_user(
        user_id=uuid.uuid4(),
        subject="sub-monitoring",
        email="operator@example.test",
        role=role,
        tenant_id=tenant,
    )


class TestTheCatalogueRefusesAnUnscopedTemplateAtImport:
    """Structural, and it fails at import rather than at request time.

    The ArgoCD lesson: a missing safety element that merely renders as absent is one nobody notices. A
    tenant-scoped entry whose template forgot the matcher would return every tenant's series and look
    entirely correct in review.
    """

    def test_a_scoped_entry_without_the_slot_is_refused(self) -> None:
        with pytest.raises(CatalogueError, match="no .* slot"):
            CatalogueEntry(
                name="leaky",
                kind="instant",
                template="sum(gen_ai_cost_total)",  # no tenant matcher
            )

    def test_an_unscoped_entry_must_state_its_reason(self) -> None:
        with pytest.raises(CatalogueError, match="without a stated reason"):
            CatalogueEntry(
                name="unscoped-no-reason",
                kind="instant",
                template="sum(up)",
                tenant_scoped=False,
            )

    def test_a_declared_parameter_the_template_ignores_is_refused(self) -> None:
        """An ignored parameter lets a caller believe they filtered when they did not."""
        with pytest.raises(CatalogueError, match="never uses"):
            CatalogueEntry(
                name="ignores-its-parameter",
                kind="instant",
                template="sum(up)",
                parameters=("window",),
                tenant_scoped=False,
                unscoped_reason="test",
            )

    def test_every_shipped_entry_is_scoped_or_states_why_not(self) -> None:
        for entry in CATALOGUE.values():
            if entry.tenant_scoped:
                assert TENANT_SLOT in entry.template, entry.name
            else:
                assert entry.unscoped_reason, entry.name
                # The reason must be a sentence, not a word. "n/a" is how this check gets defeated.
                assert len(entry.unscoped_reason) > 40, entry.name

    def test_every_entry_describes_itself_in_words(self) -> None:
        """A panel renders `describes`; an empty one leaves a human inferring meaning from a series name."""
        for entry in CATALOGUE.values():
            assert len(entry.describes) > 20, entry.name


class TestACraftedArgumentCannotEscapeItsMatcher:
    """Rendering-level. The store-level proof is further down."""

    @pytest.mark.parametrize(
        "hostile",
        [
            'x"} or gen_ai_cost_total{',  # close the matcher, open another
            'x", forgeops_tenant_id!="',  # add a negated matcher
            "x} or vector(1) # ",
            "x\\",  # trailing escape, to swallow the closing quote
            'x"',
            "x{y}",
            "x,y",
            "x(y)",
            "x or y",  # a space is not permitted at all, which kills every injection needing two tokens
        ],
    )
    def test_hostile_label_values_are_refused(self, hostile: str) -> None:
        entry = CATALOGUE["http_request_rate"]
        with pytest.raises(ValueError, match="not permitted in a label value"):
            entry.render(tenant_id=None, arguments={"window": hostile})

    def test_an_undeclared_argument_is_refused_rather_than_ignored(self) -> None:
        entry = CATALOGUE["ai_cost_by_model"]
        with pytest.raises(ValueError, match="does not accept"):
            entry.render(tenant_id=uuid.uuid4(), arguments={"forgeops_tenant_id": "other"})

    def test_an_absent_tenant_becomes_the_none_label_and_not_an_empty_matcher(self) -> None:
        """An empty matcher would select every series, for the principal most likely to lack a tenant."""
        rendered = CATALOGUE["ai_cost_total"].render(tenant_id=None, arguments={})
        assert 'forgeops_tenant_id="none"' in rendered

    def test_the_tenant_matcher_is_present_in_every_rendered_scoped_query(self) -> None:
        tenant = uuid.uuid4()
        for entry in CATALOGUE.values():
            if not entry.tenant_scoped:
                continue
            arguments = {name: "5m" if name == "window" else "x" for name in entry.parameters}
            rendered = entry.render(tenant_id=tenant, arguments=arguments)
            assert f'forgeops_tenant_id="{tenant}"' in rendered, entry.name


class TestTheRouteAcceptsNoPromQL:
    @pytest.mark.asyncio
    async def test_a_query_field_is_refused_rather_than_ignored(self) -> None:
        """`extra: forbid`. An ignored field is indistinguishable from an honoured one, from outside."""
        app = _app(principal=_principal(), reader=MetricsReader(base_url=""))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/monitoring/query",
                json={"name": "ai_cost_total", "query": "up", "arguments": {}},
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_an_unknown_name_does_not_echo_it_back(self) -> None:
        """Echoing the name would make this a probe for which metrics exist in the store."""
        app = _app(principal=_principal(), reader=MetricsReader(base_url=""))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/monitoring/query",
                json={"name": "node_filesystem_free_bytes", "arguments": {}},
            )
        assert response.status_code == 422
        assert "node_filesystem_free_bytes" not in response.text

    @pytest.mark.asyncio
    async def test_a_viewer_cannot_run_a_deployment_wide_query(self) -> None:
        app = _app(principal=_principal(role=UserRole.VIEWER), reader=MetricsReader(base_url=""))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/monitoring/query",
                json={"name": "http_request_rate", "arguments": {"window": "5m"}},
            )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_a_window_outside_the_allowed_set_is_refused(self) -> None:
        """`999999d` is a cheap way to make the store do unbounded work for an ordinary caller."""
        app = _app(principal=_principal(), reader=MetricsReader(base_url="http://127.0.0.1:1"))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/monitoring/query",
                json={"name": "ai_cost_series", "arguments": {}, "window": "999999d"},
            )
        assert response.status_code == 422


class TestTheVerdictDistinguishesTheThreeStates:
    """Absence of health is not health. Each state is a different sentence, not a different number."""

    @pytest.mark.asyncio
    async def test_unconfigured_is_not_zero(self) -> None:
        app = _app(principal=_principal(), reader=MetricsReader(base_url=""))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/monitoring/query", json={"name": "ai_cost_total", "arguments": {}})
        body = response.json()
        assert body["verdict"] == "unconfigured"
        assert body["has_numbers"] is False
        assert body["series"] == []
        assert "not" in body["explanation"].lower()

    @pytest.mark.asyncio
    async def test_unreachable_is_distinct_from_unconfigured(self) -> None:
        # Port 1 is not listening, so this is a real connection failure rather than a stub.
        app = _app(principal=_principal(), reader=MetricsReader(base_url="http://127.0.0.1:1", timeout_seconds=2.0))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/monitoring/query", json={"name": "ai_cost_total", "arguments": {}})
        body = response.json()
        assert body["verdict"] == "unreachable"
        assert body["has_numbers"] is False
        assert "did not answer" in body["explanation"]

    @pytest.mark.asyncio
    async def test_readiness_says_which_of_the_two_it_is(self) -> None:
        app = _app(principal=_principal(), reader=MetricsReader(base_url=""))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/monitoring/readiness")
        body = response.json()
        assert body["metrics_store_configured"] is False
        assert "No metrics store is configured" in body["explanation"]


@pytest.mark.skipif(
    not PROM_URL,
    reason=(
        "PLATFORM: needs a running Prometheus. Set FORGEOPS_TEST_PROMETHEUS_URL, which "
        "`docker compose --profile observability up -d prometheus` provides."
    ),
)
class TestAgainstARealStoreACallerCannotSeeAnotherTenant:
    """The claim that matters, proven by the store refusing rather than by the string looking right."""

    @pytest.mark.asyncio
    async def test_the_store_returns_only_the_callers_tenant(self) -> None:
        # Two tenants are written into Prometheus by the harness that sets FORGEOPS_TEST_PROMETHEUS_URL;
        # their ids arrive here so the test can assert on real stored series.
        mine = os.environ["FORGEOPS_TEST_TENANT_A"]
        theirs = os.environ["FORGEOPS_TEST_TENANT_B"]

        reader = MetricsReader(base_url=PROM_URL)
        app = _app(principal=_principal(tenant=uuid.UUID(mine)), reader=reader)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/monitoring/query",
                json={"name": "ai_cost_by_model", "arguments": {}},
            )
        body = response.json()
        assert response.status_code == 200, body

        # The other tenant's id must appear nowhere in the response -- not in a label, not in the promql.
        assert theirs not in response.text

        # And the store must actually hold the other tenant's series, or this proves nothing: a query that
        # returned nothing for both tenants would pass the assertion above vacuously.
        async with httpx.AsyncClient(timeout=10.0) as raw:
            everything = await raw.post(
                f"{PROM_URL}/api/v1/query",
                data={"query": "tenant_model:gen_ai_cost:increase24h"},
            )
        assert theirs in everything.text, (
            "the other tenant's series is not in the store, so the isolation assertion above was vacuous"
        )
