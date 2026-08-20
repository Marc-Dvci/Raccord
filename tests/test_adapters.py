"""The translation layer between capabilities and a real server's tool schemas.

These tests pin the shapes recorded from the official grafana/mcp-grafana server
on 6 August 2026 (docs/mcp_conformance.json, docs/real_mcp_run.json). They are
regression tests in the strict sense: when the server changes again - and it
will, it already has twice - one of these fails and says which translation
stopped being true, instead of an investigation dying halfway through.
"""

from __future__ import annotations

import json

import pytest

from raccord.grafana_mcp.adapters import (
    AdapterContext,
    AdapterError,
    adapter_for,
)

CTX = AdapterContext(
    grafana_url="http://localhost:3000",
    datasources={"prometheus": "raccord-prom", "loki": "raccord-loki", "tempo": "raccord-tempo"},
)


# -- Prometheus -------------------------------------------------------------


def test_prometheus_request_uses_rfc3339_without_fractional_seconds():
    """Grafana's time parser rejects the microseconds Python's isoformat emits."""
    a = adapter_for("query_prometheus", "query_prometheus")
    sent = a.request(
        {
            "expr": "raccord_caption_drift_seconds",
            "start": "2026-08-06T17:13:24.143415+00:00",
            "end": "2026-08-06T17:33:24.143415+00:00",
            "aggregation": "max",
        },
        CTX,
    )
    assert sent["datasourceUid"] == "raccord-prom"
    assert sent["startTime"] == "2026-08-06T17:13:24Z"
    assert sent["endTime"] == "2026-08-06T17:33:24Z"
    assert "." not in sent["startTime"]
    assert sent["stepSeconds"] >= 10


def test_prometheus_response_reduces_matrix_to_the_requested_aggregate():
    a = adapter_for("query_prometheus", "query_prometheus")
    raw = json.dumps(
        {
            "data": [
                {"metric": {"territory": "FR"}, "values": [[1, "0.5"], [2, "8.0"], [3, "2.0"]]},
                {"metric": {"territory": "JP"}, "values": [[1, "0.1"], [2, "0.2"]]},
            ]
        }
    )
    out = a.response(raw, {"expr": "x", "aggregation": "max"})
    assert out["resultCount"] == 2
    # Worst first: the agents read result[0] as the worst observed slice.
    assert out["result"][0]["metric"]["territory"] == "FR"
    assert out["result"][0]["value"] == 8.0

    last = a.response(raw, {"expr": "x", "aggregation": "last"})
    assert last["result"][0]["value"] == 2.0


def test_prometheus_requires_a_prometheus_datasource():
    a = adapter_for("query_prometheus", "query_prometheus")
    with pytest.raises(AdapterError):
        a.request({"expr": "x"}, AdapterContext(datasources={"loki": "raccord-loki"}))


# -- Loki -------------------------------------------------------------------


def test_loki_request_renames_expr_to_logql():
    a = adapter_for("query_loki_logs", "query_loki_logs")
    sent = a.request({"expr": '{service="capenc-pool-a"}', "limit": 40}, CTX)
    assert sent["logql"] == '{service="capenc-pool-a"}'
    assert sent["datasourceUid"] == "raccord-loki"
    assert sent["limit"] == 40


def test_loki_response_normalises_entries():
    a = adapter_for("query_loki_logs", "query_loki_logs")
    raw = json.dumps(
        {
            "data": [
                {
                    "timestamp": "2026-08-06T17:20:00Z",
                    "labels": {"service": "capenc-pool-a"},
                    "line": 'msg="caption pts reanchor"',
                },
            ]
        }
    )
    out = a.response(raw, {"expr": "q"})
    assert out["resultCount"] == 1
    assert out["result"][0]["line"].startswith("msg=")
    assert out["result"][0]["labels"]["service"] == "capenc-pool-a"


def test_loki_empty_result_is_not_an_error():
    """The server answers a no-match query with hints, not with a failure."""
    a = adapter_for("query_loki_logs", "query_loki_logs")
    out = a.response(json.dumps({"data": [], "hints": {"summary": "no entries"}}), {"expr": "q"})
    assert out["resultCount"] == 0
    assert out["result"] == []


# -- Alerting: one dispatch tool, two capabilities ---------------------------


def test_alert_list_recovers_the_uid_the_server_omits():
    a = adapter_for("list_alert_rules", "alerting_manage_rules")
    raw = json.dumps(
        [
            {
                "uid": "",
                "title": "End-to-end caption drift outside objective",
                "state": "firing",
                "rule_group": "accessibility-slo",
                "labels": {"slo": "cap.drift", "feature": "captions"},
                "annotations": {"slo_objective": "1.5"},
                "data": [{"model": {"expr": "max by (territory) (x) > 1.5"}}],
            }
        ]
    )
    out = a.response(raw, {"limit": 50})
    assert out[0]["uid"] == "raccord-cap-drift"
    assert out[0]["state"] == "firing"
    assert out[0]["labels"]["slo"] == "cap.drift"
    assert out[0]["query"].startswith("max by")


def test_alert_list_translates_alerting_to_firing():
    """Some builds say "alerting" where the agents look for "firing"."""
    a = adapter_for("list_alert_rules", "alerting_manage_rules")
    out = a.response(json.dumps([{"state": "alerting", "labels": {"slo": "cap.drift"}}]), {})
    assert out[0]["state"] == "firing"


def test_alert_list_leaves_uid_empty_when_it_cannot_be_recovered():
    """Better an unfetchable rule than an invented uid."""
    a = adapter_for("list_alert_rules", "alerting_manage_rules")
    out = a.response(json.dumps([{"uid": "", "labels": {}, "title": "someone else's rule"}]), {})
    assert out[0]["uid"] == ""


def test_alert_get_dispatches_on_operation():
    a = adapter_for("get_alert_rule", "alerting_manage_rules")
    assert a.request({"uid": "raccord-cap-drift"}, CTX) == {
        "operation": "get",
        "rule_uid": "raccord-cap-drift",
    }
    with pytest.raises(AdapterError):
        a.request({}, CTX)


# -- Annotations ------------------------------------------------------------


def test_annotations_request_uses_epoch_ms_and_matches_any_tag():
    a = adapter_for("find_annotations", "get_annotations")
    sent = a.request(
        {
            "start": "2026-08-06T17:00:00+00:00",
            "end": "2026-08-06T17:20:00+00:00",
            "tags": ["deployment", "config", "change"],
        },
        CTX,
    )
    assert sent["from"] == 1786035600000
    assert sent["to"] == 1786036800000
    # AND across three change kinds would return nothing.
    assert sent["matchAny"] is True


def test_annotations_response_unwraps_payload_and_converts_time():
    a = adapter_for("find_annotations", "get_annotations")
    out = a.response(
        json.dumps(
            {
                "Payload": [
                    {
                        "id": 33,
                        "time": 1786037499066,
                        "timeEnd": 1786037499066,
                        "text": "PTP grandmaster failover to NTP fallback pool",
                        "tags": ["config", "change"],
                    },
                ]
            }
        ),
        {},
    )
    assert out[0]["id"] == 33
    assert out[0]["text"].startswith("PTP grandmaster")
    assert out[0]["time"].startswith("2026-08-06T")


# -- Dashboards, deep links -------------------------------------------------


def test_search_dashboards_unwraps_the_envelope():
    a = adapter_for("search_dashboards", "search_dashboards")
    out = a.response(
        json.dumps(
            {
                "dashboards": [
                    {
                        "uid": "raccord-incident",
                        "title": "Raccord / Incident investigation",
                        "tags": ["raccord"],
                        "url": "/d/raccord-incident/x",
                    }
                ],
                "total": 1,
            }
        ),
        {},
    )
    assert out[0]["uid"] == "raccord-incident"


def test_deeplink_response_accepts_a_bare_url_string():
    """This tool answers with a URL, not with JSON."""
    a = adapter_for("generate_deeplink", "generate_deeplink")
    out = a.response(
        "http://localhost:3000/d/raccord-incident?from=now-20m&to=now",
        {"resourceType": "dashboard"},
    )
    assert out["url"].startswith("http://localhost:3000/d/raccord-incident")


# -- Traces: no Tempo tool exists, so route through the datasource proxy -----


def test_tempo_request_targets_the_datasource_proxy():
    a = adapter_for("query_tempo_traces", "grafana_api_request")
    sent = a.request({"service": "media-path", "limit": 10}, CTX)
    assert sent["endpoint"].startswith("/api/datasources/proxy/uid/raccord-tempo/api/search")
    assert 'resource.service.name="media-path"' in sent["endpoint"]
    assert sent["method"] == "GET"


def test_tempo_response_normalises_to_the_canonical_trace_shape():
    a = adapter_for("query_tempo_traces", "grafana_api_request")
    out = a.response(
        json.dumps(
            {
                "status": 200,
                "data": {
                    "traces": [
                        {
                            "traceID": "fa194dff",
                            "rootServiceName": "media-path",
                            "rootTraceName": "media.deliver",
                            "durationMs": 199,
                            "startTimeUnixNano": "1786037535098872832",
                        },
                    ]
                },
            }
        ),
        {},
    )
    assert out["resultCount"] == 1
    assert out["traces"][0]["name"] == "media.deliver"
    assert out["traces"][0]["durationMs"] == 199
    assert out["traces"][0]["attributes"]["component"] == "media-path"


def test_tempo_requires_a_tempo_datasource():
    a = adapter_for("query_tempo_traces", "grafana_api_request")
    with pytest.raises(AdapterError):
        a.request({"service": "media-path"}, AdapterContext(datasources={}))


# -- Incidents --------------------------------------------------------------


def test_create_incident_converts_labels_to_the_list_form_irm_expects():
    a = adapter_for("create_incident", "create_incident")
    sent = a.request({"title": "t", "severity": "sev1", "labels": {"slo": "cap.drift"}}, CTX)
    assert sent["labels"] == [{"label": "slo:cap.drift"}]


# -- The registry itself ----------------------------------------------------


def test_unadapted_capabilities_pass_through():
    """This module records deviations; a server already speaking canon needs none."""
    assert adapter_for("list_datasources", "list_datasources") is None
    assert adapter_for("query_prometheus", "some_other_server_tool") is None
