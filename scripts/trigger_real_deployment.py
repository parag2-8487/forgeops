import asyncio
import json
import uuid
from sqlalchemy import text
from src.main import create_app
from src.auth.principal import Principal
from src.auth.models import UserRole
from src.deployments.service import DeploymentService

async def main():
    app = create_app()
    async with app.router.lifespan_context(app):
        maker = app.state.sessionmaker
        chokepoint = app.state.governance_chokepoint
        deployments = DeploymentService()

        project_id = uuid.UUID("1214c0cb-47e5-4c2f-9f8e-c81788978e89")
        user_id = uuid.UUID("fe5aa283-2f7e-43cc-a440-259e09a53dae")
        env_id = uuid.UUID("6a8fd4f7-7baa-42d7-8f57-4a7450fded5a")

        async with maker() as session:
            user_row = (await session.execute(
                text("SELECT idp_subject, email, tenant_id FROM users WHERE id = :id"),
                {"id": user_id}
            )).mappings().one()

            principal = Principal.for_user(
                user_id=user_id,
                subject=user_row["idp_subject"],
                email=user_row["email"],
                role=UserRole.ADMIN,
                tenant_id=user_row["tenant_id"],
            )

            manifests = ["docker-compose.yml"]
            record = await deployments.create(
                session,
                project_id=project_id,
                environment_id=env_id,
                tenant_id=principal.tenant_id,
                manifests=manifests,
                cluster_context=None,
                namespace=None,
                requested_by=principal.user_id,
            )

            submission = await chokepoint.deploy_manifests(
                session,
                project_id=project_id,
                principal=principal,
                deployment_id=record.id,
                environment_name="staging",
                environment_requires_approval=False,
                manifests=list(record.manifests),
                cluster_context=record.cluster_context,
                namespace=record.namespace,
                health_timeout_seconds=90,
                reason="real deployment verification",
                auto_approve=True,
            )

            await deployments.attach_change_set(
                session,
                deployment_id=record.id,
                change_set_id=submission.change_set_id,
                status="applying" if submission.status == "applying" else "pending_approval",
            )
            await session.commit()

            print(json.dumps({
                "deployment_id": str(record.id),
                "change_set_id": str(submission.change_set_id),
                "status": submission.status,
                "outcome": submission.outcome,
            }))

if __name__ == "__main__":
    asyncio.run(main())
