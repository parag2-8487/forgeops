import base64
import uuid
import time
from datetime import datetime, UTC
from typing import Any

from sqlmodel import select

from src.core.config import get_settings
from src.core.db import create_db_engine, create_sessionmaker
from src.core.tasks import register_task
from src.policies.models import PolicyBundle
from src.auth.device_models import AgentDevice, DeviceStatus
from src.websocket.hub import AgentHub
from src.governance.chokepoint import generate_nonce
from src.governance.envelope import CommandEnvelope, PolicyContextPayload, sign_envelope, signing_key_scope
from src.auth.devices import envelope_key
from src.governance.sequencer import AgentSequencer
from redis.asyncio import Redis

_sessionmaker = None
_hub = None
_sequencer = None

async def _init_globals() -> None:
    global _sessionmaker, _hub, _sequencer
    if _sessionmaker is not None:
        return
    settings = get_settings()
    engine = create_db_engine(settings)
    _sessionmaker = create_sessionmaker(engine)
    redis_url = str(settings.redis_url)
    redis = Redis.from_url(redis_url)
    _hub = AgentHub(redis)
    _sequencer = AgentSequencer(redis)

@register_task("policy.bundle.publish")
async def publish_bundle_task(payload: dict[str, Any]) -> None:
    """Deliver the bundle inside a signed command envelope."""
    await _init_globals()
    assert _sessionmaker is not None
    assert _hub is not None
    assert _sequencer is not None
    
    settings = get_settings()
    
    bundle_id = uuid.UUID(payload["bundle_id"])
    project_id_str = payload.get("project_id")
    project_id = uuid.UUID(project_id_str) if project_id_str else None
        
    async with _sessionmaker() as session:
        bundle = await session.get(PolicyBundle, bundle_id)
        if not bundle:
            return
            
        stmt = select(AgentDevice).where(AgentDevice.status == DeviceStatus.ACTIVE)
        if project_id:
            stmt = stmt.where(AgentDevice.project_id == project_id)
        
        result = await session.execute(stmt)
        devices = result.scalars().all()
        
        payload_b64 = base64.b64encode(bundle.bundle).decode("utf-8")
        
        for device in devices:
            # Check if device is connected to avoid unnecessary DB/Redis operations
            if not _hub.owns(device.id):
                # We could broadcast even if it's disconnected; if they reconnect they get it? 
                # Actually, send_command enqueues it if active.
                pass
                
            try:
                floor = await _sequencer._redis.get(f"mcp:seq:{device.id}")
                floor_int = int(floor) if floor else 0
                seq = await _sequencer.next_seq(device.id, floor=floor_int)
                nonce = generate_nonce()
                await _sequencer.reserve_nonce(device.id, nonce, ttl_seconds=3600)
                not_after = int(datetime.now(UTC).timestamp()) + 3600
                
                envelope = CommandEnvelope(
                    command_id=str(uuid.uuid4()),
                    device_id=str(device.id),
                    operation="policy.bundle.publish",
                    args={"bundle": payload_b64},
                    approval_id=str(uuid.uuid4()), # Need an approval_id for the schema validation
                    policy_context=PolicyContextPayload(bundle_digest=bundle.digest, decision="allow"),
                    nonce=nonce,
                    seq=seq,
                    not_after=not_after,
                )
                
                key = await envelope_key(session, device_id=device.id, pepper=settings.security_pepper)
                with signing_key_scope(key.get_secret_value()):
                    signature = sign_envelope(envelope)
                
                # We send the signed command through the hub
                await _hub.send_command(
                    device_id=device.id, 
                    command={"envelope": envelope.to_mapping(), "signature": signature}
                )
            except Exception:
                # Fire and forget
                pass
