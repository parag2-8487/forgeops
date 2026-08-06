#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Merge tree-sitter Wasm grammar components into CycloneDX SBOM (Leaf 11.1)."""

import json
import os
import sys

def merge_sbom(sbom_path, lock_path):
    if not os.path.exists(lock_path):
        print(f"FAIL: lockfile not found at {lock_path}", file=sys.stderr)
        sys.exit(1)

    with open(lock_path, "r", encoding="utf-8") as f:
        lock_data = json.load(f)

    grammars = lock_data.get("grammars", {})
    if not grammars:
        print("FAIL: no grammars found in lockfile", file=sys.stderr)
        sys.exit(1)

    if os.path.exists(sbom_path):
        with open(sbom_path, "r", encoding="utf-8") as f:
            sbom_data = json.load(f)
    else:
        sbom_data = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "version": 1,
            "components": []
        }

    components = sbom_data.get("components", [])
    existing_purls = {c.get("purl") for c in components if "purl" in c}

    added = 0
    for name, meta in grammars.items():
        purl = meta.get("purl")
        if purl and purl not in existing_purls:
            comp = {
                "type": "library",
                "name": meta.get("name", f"tree-sitter-{name}"),
                "version": meta.get("version", "0.20.0"),
                "licenses": [{"license": {"id": meta.get("licence", "MIT")}}],
                "purl": purl,
                "hashes": [
                    {
                        "alg": "SHA-256",
                        "content": meta.get("sha256")
                    }
                ],
                "externalReferences": [
                    {
                        "type": "vcs",
                        "url": meta.get("source_url")
                    }
                ]
            }
            components.append(comp)
            existing_purls.add(purl)
            added += 1

    sbom_data["components"] = components

    os.makedirs(os.path.dirname(sbom_path), exist_ok=True)
    with open(sbom_path, "w", encoding="utf-8") as f:
        json.dump(sbom_data, f, indent=2)

    print(f"sbom-merge: successfully merged {added} grammar components into {sbom_path}")

if __name__ == "__main__":
    sbom_p = sys.argv[1] if len(sys.argv) > 1 else "agent/dist/forgeops-agent.sbom.json"
    lock_p = sys.argv[2] if len(sys.argv) > 2 else "agent/internal/scanner/grammars/grammars.lock.json"
    merge_sbom(sbom_p, lock_p)
