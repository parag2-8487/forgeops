# SPDX-License-Identifier: FSL-1.1-ALv2
"""Secret storage backends (Infisical and Local AES-GCM)."""

import os
from typing import Protocol
import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .models import Secret

class SecretStore(Protocol):
    """Protocol for reading and writing secret values."""
    
    async def get_value(self, secret: Secret) -> str:
        """Retrieve the plaintext value of a secret."""
        ...
        
    async def set_value(self, secret: Secret, value: str) -> None:
        """Store the plaintext value and update the secret record."""
        ...
        
class InfisicalStore:
    def __init__(self, http: httpx.AsyncClient, base_url: str, client_id: str, client_secret: str):
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        
    async def _get_token(self) -> str:
        if self._token:
            return self._token
        resp = await self._http.post(
            f"{self._base_url}/api/v1/auth/universal-auth/login",
            data={"clientId": self._client_id, "clientSecret": self._client_secret}
        )
        resp.raise_for_status()
        self._token = resp.json().get("accessToken")
        return self._token or ""
        
    async def get_value(self, secret: Secret) -> str:
        if not secret.infisical_path:
            raise ValueError("Secret missing infisical_path")
        
        token = await self._get_token()
        resp = await self._http.get(
            f"{self._base_url}/api/v3/secrets/raw/{secret.key}",
            params={
                "workspaceId": str(secret.project_id),
                "environment": secret.environment,
                "secretPath": secret.infisical_path or "/",
            },
            headers={"Authorization": f"Bearer {token}"}
        )
        if resp.status_code == 404:
            raise KeyError(f"Secret {secret.key} not found in Infisical")
        resp.raise_for_status()
        return resp.json()["secret"]["secretValue"]
        
    async def set_value(self, secret: Secret, value: str) -> None:
        path = secret.infisical_path or "/"
        token = await self._get_token()
        resp = await self._http.post(
            f"{self._base_url}/api/v3/secrets/raw/{secret.key}",
            json={
                "workspaceId": str(secret.project_id),
                "environment": secret.environment,
                "secretPath": path,
                "secretValue": value,
            },
            headers={"Authorization": f"Bearer {token}"}
        )
        resp.raise_for_status()
        secret.encrypted_value = None
        secret.infisical_path = path

class LocalSealedStore:
    def __init__(self, master_key: bytes):
        if len(master_key) != 32:
            raise ValueError("Master key must be exactly 32 bytes for AES-256-GCM")
        self._aesgcm = AESGCM(master_key)
        
    async def get_value(self, secret: Secret) -> str:
        if not secret.encrypted_value:
            raise ValueError("Secret missing encrypted_value")
        # Extract nonce and ciphertext
        nonce = secret.encrypted_value[:12]
        ciphertext = secret.encrypted_value[12:]
        plaintext = self._aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode("utf-8")
        
    async def set_value(self, secret: Secret, value: str) -> None:
        nonce = os.urandom(12)
        ciphertext = self._aesgcm.encrypt(nonce, value.encode("utf-8"), None)
        secret.encrypted_value = nonce + ciphertext
        secret.infisical_path = None
