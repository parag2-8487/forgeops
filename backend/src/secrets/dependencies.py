from fastapi import Request
from .store import SecretStore

def get_secret_store(request: Request) -> SecretStore:
    return request.app.state.secret_store
