import asyncio
import json
import time
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
        redis_client = app.state.agent_hub._deps.redis
        
        project_id = uuid.UUID("d782e0e4-c5ea-4276-a454-171ddc31a5ca")
        user_id = uuid.UUID("fe5aa283-2f7e-43cc-a440-259e09a53dae")
        env_id = uuid.UUID("532bd099-1869-404d-87df-bbff3796f070")
        
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
                reason="Phase 2 live verification",
                auto_approve=True,
            )
            
            await deployments.attach_change_set(
                session,
                deployment_id=record.id,
                change_set_id=submission.change_set_id,
                status="applying" if submission.status == "applying" else "pending_approval",
            )
            await session.commit()
            
            cs_row = (await session.execute(
                text("SELECT command_id FROM change_sets WHERE id = :id"),
                {"id": submission.change_set_id}
            )).first()
            command_id = str(cs_row[0]) if cs_row and cs_row[0] else None

            print("DEPLOYMENT_STARTED", json.dumps({
                "deployment_id": str(record.id),
                "change_set_id": str(submission.change_set_id),
                "command_id": command_id,
                "initial_status": submission.status,
            }))

        # Monitor settlement
        start_time = time.time()
        timeout = 600  # 10 minutes max
        terminal_statuses = {"applied", "failed", "degraded", "rolled_back"}

        while time.time() - start_time < timeout:
            await asyncio.sleep(5)
            async with maker() as session:
                dep_row = (await session.execute(
                    text("SELECT status, healthy, stable, completed_at FROM deployments WHERE id = :id"),
                    {"id": record.id}
                )).mappings().first()
                
                cs_row = (await session.execute(
                    text("SELECT status, applied_at FROM change_sets WHERE id = :id"),
                    {"id": submission.change_set_id}
                )).mappings().first()

                elapsed = int(time.time() - start_time)
                print(f"[{elapsed}s] Deployment status: {dep_row['status']}, healthy: {dep_row['healthy']}, stable: {dep_row['stable']} | ChangeSet status: {cs_row['status']}")

                if dep_row["status"] in terminal_statuses:
                    print("DEPLOYMENT_TERMINAL", json.dumps({
                        "deployment_id": str(record.id),
                        "deployment_status": dep_row["status"],
                        "healthy": dep_row["healthy"],
                        "stable": dep_row["stable"],
                        "completed_at": str(dep_row["completed_at"]),
                        "change_set_status": cs_row["status"],
                        "applied_at": str(cs_row["applied_at"]),
                    }))
                    return

        print("TIMEOUT: Deployment did not reach terminal state within 10 minutes")

if __name__ == "__main__":
    asyncio.run(main())
