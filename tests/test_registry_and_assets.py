"""Promise versioning, twin traversal, and generated Grafana assets."""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from raccord.contracts import FeatureType
from raccord.registry import PromiseRegistry, seed_promises
from raccord.twin import attach_promises, build_reference_twin

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def registry(tmp_path):
    return PromiseRegistry(tmp_path / "promises.db")


def test_promises_are_versioned_and_readable_at_a_point_in_time(registry):
    seed_promises(registry)
    original = registry.current("pr-captions-en")
    assert original.version == 1

    amended = registry.amend(
        "pr-captions-en",
        "a11y-lead",
        "festival agreed a wider drift tolerance",
        max_sync_drift_ms=2500,
    )
    assert amended.version == 2
    assert amended.effective_from > original.effective_from
    assert registry.current("pr-captions-en").max_sync_drift_ms == 2500

    # Incident reasoning must resolve the version in force at the incident time,
    # not the version in force now.
    during_v1 = amended.effective_from - timedelta(microseconds=1)
    historical = registry.as_of("pr-captions-en", during_v1)
    assert historical.version == 1
    assert historical.max_sync_drift_ms == 1500

    # The boundary belongs to the new version: [effective_from, effective_to)
    assert registry.as_of("pr-captions-en", amended.effective_from).version == 2


def test_a_promise_cannot_be_registered_twice(registry):
    seed_promises(registry)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(registry.current("pr-captions-en"))


def test_matching_filters_by_audience_slice(registry):
    seed_promises(registry)
    sign = registry.matching(
        "evt-lumiere-premiere", feature=FeatureType.SIGN_LANGUAGE, territory="FR"
    )
    assert [p.promise_id for p in sign] == ["pr-sign-lsf"]
    assert not registry.matching(
        "evt-lumiere-premiere", feature=FeatureType.SIGN_LANGUAGE, territory="JP"
    )


def test_blast_radius_finds_the_promises_riding_on_a_component(registry):
    twin = build_reference_twin()
    promises = seed_promises(registry)
    attach_promises(twin, promises)

    br = twin.blast_radius(["capenc-pool-a"])
    assert any("captions" in p for p in br.promise_ids)
    assert "capenc-pool-b" in br.safe_remediation_targets
    assert "eu-west" in br.cdn_regions


def test_twin_versioning_keeps_history():
    twin = build_reference_twin()
    original = twin.node("capenc-pool-a")
    twin.upsert_node("capenc-pool-a", "caption_encoder_pool", "pool A (rebuilt)", {"nodes": 8})
    current = twin.node("capenc-pool-a")
    assert current.version == 2
    assert current.effective_from > original.effective_from

    historical = twin.node("capenc-pool-a", current.effective_from - timedelta(microseconds=1))
    assert historical.version == 1
    assert historical.attrs["nodes"] == 4


def test_generated_grafana_assets_match_the_slo_definitions():
    """Dashboards and alert rules are generated from raccord/slo.py.
    If this fails, run tools/generate_grafana_assets.py."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "generate_grafana_assets.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_slo_has_a_provisioned_alert_rule():
    import yaml

    from raccord.slo import ALL_SLOS

    path = ROOT / "observability" / "grafana" / "provisioning" / "alerting" / "raccord-rules.yml"
    rules = yaml.safe_load(path.read_text(encoding="utf-8"))
    uids = {r["uid"] for r in rules["groups"][0]["rules"]}
    for s in ALL_SLOS:
        assert f"raccord-{s.slo_id.replace('.', '-')}" in uids, s.slo_id


def test_every_fault_declares_a_remediation_the_catalog_can_perform():
    from raccord.contracts import ActionType
    from raccord.faults import FAULT_LIBRARY

    valid = {a.value for a in ActionType}
    for fault in FAULT_LIBRARY.values():
        assert fault.remediation, f"{fault.fault_id} has no remediation"
        for action in fault.remediation:
            assert action in valid, (fault.fault_id, action)


def test_every_failure_class_a_fault_can_produce_has_a_mapped_action():
    from raccord.agents import REMEDIATION_MAP
    from raccord.faults import FAULT_LIBRARY

    for fault in FAULT_LIBRARY.values():
        assert fault.failure_class in REMEDIATION_MAP, fault.failure_class


def test_demonstration_media_is_original():
    from raccord.media import MEDIA_MANIFEST

    assert MEDIA_MANIFEST["third_party_content"] == "none"
    assert "original" in MEDIA_MANIFEST["origin"]
