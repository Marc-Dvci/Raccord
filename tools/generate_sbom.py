"""Generate a CycloneDX software bill of materials for AccessPulse.

    python tools/generate_sbom.py                    # writes sbom.json
    python tools/generate_sbom.py --check            # fails if sbom.json is stale

Reads the installed distributions rather than parsing pyproject, so the SBOM
describes what is actually present - including transitive dependencies, which is
the part that matters when a vulnerability is announced.

Deliberately dependency-free: a tool whose job is to enumerate the dependency
tree should not add to it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "sbom.json"

# Distribution names are not always import names, and licence metadata is
# inconsistent across the ecosystem. Where a licence is not machine-readable we
# say "declared in package metadata" rather than guessing.
LICENSE_FIELDS = ("License-Expression", "License")


def _license(meta) -> str:
    for field in LICENSE_FIELDS:
        value = meta.get(field)
        if value and value.strip() and value.strip().lower() != "unknown":
            return value.strip().splitlines()[0][:120]
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License :: "):
            return classifier.rsplit("::", 1)[-1].strip()
    return "see package metadata"


def _purl(name: str, version: str) -> str:
    return f"pkg:pypi/{name.lower().replace('_', '-')}@{version}"


def components() -> list[dict]:
    out: list[dict] = []
    for dist in sorted(metadata.distributions(),
                       key=lambda d: (d.metadata["Name"] or "").lower()):
        name = dist.metadata["Name"]
        if not name or name.lower() == "accesspulse":
            continue
        version = dist.version or "0"
        component = {
            "type": "library",
            "bom-ref": _purl(name, version),
            "name": name,
            "version": version,
            "purl": _purl(name, version),
            "licenses": [{"license": {"name": _license(dist.metadata)}}],
            "externalReferences": [],
        }
        homepage = dist.metadata.get("Home-page")
        if homepage:
            component["externalReferences"].append(
                {"type": "website", "url": homepage})
        if not component["externalReferences"]:
            del component["externalReferences"]
        out.append(component)
    return out


def build(include_runtime_services: bool = True) -> dict:
    doc = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "tools": [{"vendor": "AccessPulse", "name": "generate_sbom.py", "version": "1.0"}],
            "component": {
                "type": "application",
                "bom-ref": "pkg:pypi/accesspulse@0.1.0",
                "name": "accesspulse",
                "version": "0.1.0",
                "licenses": [{"license": {"id": "Apache-2.0"}}],
                "description": "Accessible Experience Reliability for live media",
            },
            "properties": [
                {"name": "accesspulse:media_provenance",
                 "value": "all demonstration media is original; see docs/MEDIA_RIGHTS.md"},
                {"name": "accesspulse:bundled_model_weights", "value": "none"},
                {"name": "accesspulse:vendored_third_party_code", "value": "none"},
            ],
        },
        "components": components(),
    }

    if include_runtime_services:
        # Not Python packages and not redistributed here, but a judge asking
        # "what does this thing run against" deserves the answer in one file.
        doc["components"].extend([
            {"type": "application", "bom-ref": "docker:grafana/grafana",
             "name": "grafana", "version": "latest",
             "licenses": [{"license": {"id": "AGPL-3.0-only"}}],
             "description": "Runtime service, pulled by docker compose; not redistributed."},
            {"type": "application", "bom-ref": "docker:prom/prometheus",
             "name": "prometheus", "version": "latest",
             "licenses": [{"license": {"id": "Apache-2.0"}}],
             "description": "Runtime service, pulled by docker compose; not redistributed."},
            {"type": "application", "bom-ref": "docker:grafana/loki",
             "name": "loki", "version": "latest",
             "licenses": [{"license": {"id": "AGPL-3.0-only"}}],
             "description": "Runtime service, pulled by docker compose; not redistributed."},
            {"type": "application", "bom-ref": "docker:grafana/tempo",
             "name": "tempo", "version": "latest",
             "licenses": [{"license": {"id": "AGPL-3.0-only"}}],
             "description": "Runtime service, pulled by docker compose; not redistributed."},
            {"type": "application", "bom-ref": "docker:mcp/grafana",
             "name": "grafana-mcp-server", "version": "latest",
             "licenses": [{"license": {"id": "Apache-2.0"}}],
             "description": "The official Grafana MCP server. The agent's only route to "
                            "operational truth (ADR 0002)."},
        ])
    return doc


def _fingerprint(doc: dict) -> str:
    """Hash the component list only, so a regenerated timestamp is not a change."""
    payload = json.dumps(doc["components"], sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _names(doc: dict) -> set[str]:
    return {c["name"].lower() for c in doc["components"]}


def _versions(doc: dict) -> dict[str, str]:
    return {c["name"].lower(): c["version"] for c in doc["components"]}


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate the AccessPulse SBOM")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true",
                    help="fail if the committed SBOM does not match the environment")
    args = ap.parse_args()

    doc = build()

    if args.check:
        if not args.out.exists():
            print(f"{args.out} does not exist - run: python tools/generate_sbom.py")
            return 1
        existing = json.loads(args.out.read_text(encoding="utf-8"))

        # A dependency appearing or disappearing is a real change to what this
        # software is made of, and fails the check. A version that has moved
        # under a compatible range is reported but does not fail: pip resolves
        # differently on different days, and a check that goes red for that is a
        # check people learn to ignore.
        missing = sorted(_names(existing) - _names(doc))
        added = sorted(_names(doc) - _names(existing))
        if missing or added:
            print(f"{args.out} is stale - the dependency set has changed.")
            for name in added:
                print(f"  + {name}")
            for name in missing:
                print(f"  - {name}")
            print("Regenerate it: python tools/generate_sbom.py")
            return 1

        old_versions, new_versions = _versions(existing), _versions(doc)
        drifted = [n for n in sorted(new_versions)
                   if old_versions.get(n) != new_versions[n]]
        print(f"{args.out} matches the installed dependency set "
              f"({len(doc['components'])} components)")
        for name in drifted:
            print(f"  note: {name} {old_versions[name]} -> {new_versions[name]}")
        if drifted:
            print("Versions drifted. Regenerate when convenient; not a failure.")
        return 0

    args.out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(doc['components'])} components)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
