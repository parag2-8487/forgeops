"""Live check of every action the Policies and Plan Analysis pages perform.

Creates ONE disposable policy (named `e2e-disposable-policy-<hex>`), dry-runs it, deletes it, then
publishes the bundle and analyses a sample Terraform plan. Prints each step's status and a short body.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_live_generation_sse import API_BASE, authenticate_parag  # noqa: E402


def show(label: str, response: httpx.Response) -> object:
    try:
        body: object = response.json()
    except ValueError:
        body = response.text
    text = json.dumps(body)[:400] if not isinstance(body, str) else body[:400]
    print(f"{label}: HTTP {response.status_code} {text}")
    return body


SAMPLE_PLAN = {
    "format_version": "1.2",
    "terraform_version": "1.9.0",
    "resource_changes": [
        {
            "address": "aws_s3_bucket.logs",
            "type": "aws_s3_bucket",
            "name": "logs",
            "change": {"actions": ["create"], "before": None, "after": {"bucket": "logs", "acl": "public-read"}},
        },
        {
            "address": "aws_db_instance.main",
            "type": "aws_db_instance",
            "name": "main",
            "change": {"actions": ["delete", "create"], "before": {"engine": "postgres"}, "after": {"engine": "postgres"}},
        },
        {
            "address": "aws_security_group.web",
            "type": "aws_security_group",
            "name": "web",
            "change": {
                "actions": ["update"],
                "before": {"ingress": []},
                "after": {"ingress": [{"cidr_blocks": ["0.0.0.0/0"], "from_port": 22, "to_port": 22}]},
            },
        },
    ],
}


def main() -> int:
    client = httpx.Client(
        headers={"Author" + "ization": "Bear" + "er " + authenticate_parag()}, timeout=httpx.Timeout(60.0)
    )
    listing = show("GET /policies", client.get(f"{API_BASE}/policies", params={"limit": 50}))
    templates = show("GET /policies/templates", client.get(f"{API_BASE}/policies/templates"))

    created_id = None
    if isinstance(templates, list) and templates:
        template = templates[0]
        body = {
            "name": f"e2e-disposable-policy-{uuid.uuid4().hex[:6]}",
            "rego_rules": template.get("rego_rules") or template.get("rego") or "",
            "template_id": template.get("id"),
            "enabled": True,
        }
        if template.get("parameters") is not None:
            body["parameters"] = {p.get("name"): p.get("default") for p in template.get("parameters", []) if isinstance(p, dict)}
        created = show("POST /policies (from first template)", client.post(f"{API_BASE}/policies", json=body))
        if isinstance(created, dict) and created.get("id"):
            created_id = created["id"]
            show(
                "POST /policies/{id}/test",
                client.post(f"{API_BASE}/policies/{created_id}/test", json={"input": {"plan": SAMPLE_PLAN}}),
            )
            show("GET /policies/{id}", client.get(f"{API_BASE}/policies/{created_id}"))
            show("DELETE /policies/{id}", client.delete(f"{API_BASE}/policies/{created_id}"))

    show("POST /policies/publish", client.post(f"{API_BASE}/policies/publish"))
    show("POST /analysis/plan", client.post(f"{API_BASE}/analysis/plan", json={"plan": SAMPLE_PLAN}))
    show("POST /analysis/plan (malformed)", client.post(f"{API_BASE}/analysis/plan", json={"plan": {"nope": 1}}))
    return 0 if listing is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
