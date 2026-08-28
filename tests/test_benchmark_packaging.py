import json

from raccord import api


async def test_benchmark_endpoint_reads_installed_package_copy(monkeypatch, tmp_path):
    summary = {"scenarios": 1000, "proof": "packaged"}
    package_copy = tmp_path / "raccord" / "data" / "benchmark_summary.json"
    package_copy.parent.mkdir(parents=True)
    package_copy.write_text(json.dumps(summary), encoding="utf-8")

    monkeypatch.setattr(api, "__file__", str(package_copy.parent.parent / "api.py"))
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: type("Settings", (), {"data_dir": tmp_path / "empty"})(),
    )
    assert await api.benchmark() == summary
