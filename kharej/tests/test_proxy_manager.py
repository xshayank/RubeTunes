from __future__ import annotations

import asyncio
import time
from pathlib import Path

from kharej.proxy_manager import (
    ProxyManager,
    _BACKUP_PROXY_URL,
    _MAX_CONSECUTIVE_PROXY_FAILURES,
    _ProxyRecord,
    _fetch_proxies_from_source,
    _normalize_raw_proxy_line,
    _prepare_validation_candidates,
    _validate_single_proxy,
)


class _RefillingProxyManager(ProxyManager):
    def __init__(self, cache_file: Path, *, delay: float = 0.0) -> None:
        super().__init__(sources=[], cache_file=cache_file)
        self.delay = delay
        self.refresh_calls = 0

    def _refresh(self) -> None:
        self.refresh_calls += 1
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self._proxy_records = {
                "http://127.0.0.2:8080": _ProxyRecord(speed_bps=100_000),
            }
            self._working = ["http://127.0.0.2:8080"]


def test_scan_and_get_proxy_refills_after_all_proxies_evicted(tmp_path: Path) -> None:
    mgr = _RefillingProxyManager(tmp_path / "proxies.json")
    with mgr._lock:
        mgr._proxy_records = {"http://127.0.0.1:8080": _ProxyRecord(speed_bps=100_000)}
        mgr._working = ["http://127.0.0.1:8080"]

    for _ in range(_MAX_CONSECUTIVE_PROXY_FAILURES):
        mgr.mark_proxy_failed("http://127.0.0.1:8080")

    proxy = asyncio.run(mgr.scan_and_get_proxy())

    assert proxy == _BACKUP_PROXY_URL
    assert mgr.working_count() == 1
    assert mgr.refresh_calls == 1


def test_concurrent_empty_pool_requests_share_one_refill(tmp_path: Path) -> None:
    mgr = _RefillingProxyManager(tmp_path / "proxies.json", delay=0.05)

    async def _run() -> list[str | None]:
        return await asyncio.gather(*(mgr.scan_and_get_proxy() for _ in range(5)))

    proxies = asyncio.run(_run())

    assert proxies == [_BACKUP_PROXY_URL] * 5
    assert mgr.refresh_calls == 1


def test_stale_proxy_remains_usable_as_fallback(tmp_path: Path) -> None:
    mgr = ProxyManager(cache_file=tmp_path / "proxies.json")
    with mgr._lock:
        mgr._proxy_records = {
            "http://127.0.0.1:8080": _ProxyRecord(
                speed_bps=100_000,
                last_validated_at=time.time() - 7200,
            )
        }
        mgr._working = ["http://127.0.0.1:8080"]

    assert mgr.fresh_working_count() == 0
    assert mgr.working_count() == 1
    assert mgr.get_proxy() == "http://127.0.0.1:8080"


def test_empty_pool_returns_backup_proxy_without_refreshing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("kharej.proxy_manager._fetch_raw_proxy_lists", lambda: [])
    mgr = ProxyManager(sources=[], cache_file=tmp_path / "proxies.json")

    proxy = asyncio.run(mgr.scan_and_get_proxy())

    assert proxy == _BACKUP_PROXY_URL
    assert mgr.working_count() == 1


def test_backup_proxy_is_protected_from_eviction(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("kharej.proxy_manager._fetch_raw_proxy_lists", lambda: [])
    mgr = ProxyManager(sources=[], cache_file=tmp_path / "proxies.json")

    mgr.mark_proxy_failed(_BACKUP_PROXY_URL)

    assert asyncio.run(mgr.scan_and_get_proxy()) == _BACKUP_PROXY_URL
    assert mgr.working_count() == 1


def test_proxy_failure_does_not_evict_until_threshold(tmp_path: Path) -> None:
    proxy = "http://127.0.0.1:8080"
    mgr = ProxyManager(sources=[], cache_file=tmp_path / "proxies.json")
    with mgr._lock:
        mgr._proxy_records = {proxy: _ProxyRecord(speed_bps=100_000)}
        mgr._working = [proxy]

    for _ in range(_MAX_CONSECUTIVE_PROXY_FAILURES - 1):
        mgr.mark_proxy_failed(proxy)

    assert mgr.working_count() == 1
    assert mgr.get_proxy() == proxy

    mgr.mark_proxy_failed(proxy)

    assert mgr.working_count() == 1
    assert mgr.get_proxy() == _BACKUP_PROXY_URL


def test_proxy_success_resets_failure_count(tmp_path: Path) -> None:
    proxy = "http://127.0.0.1:8080"
    mgr = ProxyManager(sources=[], cache_file=tmp_path / "proxies.json")
    with mgr._lock:
        mgr._proxy_records = {proxy: _ProxyRecord(speed_bps=100_000)}
        mgr._working = [proxy]

    for _ in range(_MAX_CONSECUTIVE_PROXY_FAILURES - 1):
        mgr.mark_proxy_failed(proxy)
    mgr.mark_proxy_succeeded(proxy)
    mgr.mark_proxy_failed(proxy)

    assert mgr.working_count() == 1
    assert mgr.get_proxy() == proxy


def test_refresh_updates_empty_pool_with_backup_proxy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("kharej.proxy_manager._fetch_raw_proxy_lists", lambda: [])
    mgr = ProxyManager(sources=[], cache_file=tmp_path / "proxies.json")

    mgr._refresh()

    assert mgr.working_count() == 1
    assert mgr.get_proxy() == _BACKUP_PROXY_URL


def test_backup_only_pool_triggers_background_refill(tmp_path: Path) -> None:
    mgr = _RefillingProxyManager(tmp_path / "proxies.json", delay=0.01)
    with mgr._lock:
        mgr._proxy_records = {_BACKUP_PROXY_URL: _ProxyRecord(speed_bps=100_000)}
        mgr._working = [_BACKUP_PROXY_URL]

    async def _run() -> str | None:
        proxy = await mgr.scan_and_get_proxy()
        await asyncio.sleep(0.05)
        return proxy

    assert asyncio.run(_run()) == _BACKUP_PROXY_URL
    assert mgr.refresh_calls == 1
    assert mgr.public_working_count() == 1


def test_fetcher_keeps_socks_proxy_schemes(monkeypatch) -> None:
    class _ProxyInfo:
        def __init__(self, protocol: str, ip: str, port: int) -> None:
            self.protocol = protocol
            self.ip = ip
            self.port = port

    class _FakeSession:
        def __init__(self, _config: dict) -> None:
            pass

        def refreshproxies(self):
            return [
                _ProxyInfo("http", "10.0.0.1", 8080),
                _ProxyInfo("https", "10.0.0.2", 8081),
                _ProxyInfo("socks4", "10.0.0.3", 1080),
                _ProxyInfo("socks5", "10.0.0.4", 1081),
            ]

    import sys
    import types

    module = types.ModuleType("freeproxy.modules")
    module.BuildProxiedSession = _FakeSession
    monkeypatch.setitem(sys.modules, "freeproxy.modules", module)

    assert _fetch_proxies_from_source("FakeSource") == [
        "http://10.0.0.1:8080",
        "http://10.0.0.2:8081",
        "socks4://10.0.0.3:1080",
        "socks5://10.0.0.4:1081",
    ]


def test_raw_proxy_line_normalizer_keeps_socks_schemes() -> None:
    assert _normalize_raw_proxy_line("socks5", "1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert _normalize_raw_proxy_line("http", "https://1.2.3.4:8443") == "http://1.2.3.4:8443"
    assert _normalize_raw_proxy_line("http", "bad-line") is None


def test_validation_accepts_fast_proxy_even_when_youtube_check_fails(monkeypatch) -> None:
    monkeypatch.setattr("kharej.proxy_manager._http_speed_check", lambda _proxy: 123_456.0)
    monkeypatch.setattr("kharej.proxy_manager._http_youtube_check", lambda _proxy: False)

    assert _validate_single_proxy("http://127.0.0.1:8080") == 123_456.0


def test_validation_candidates_skip_socks_without_pysocks(monkeypatch) -> None:
    monkeypatch.setattr("kharej.proxy_manager._socks_support_available", lambda: False)

    assert _prepare_validation_candidates(
        [
            "socks5://1.2.3.4:1080",
            "http://5.6.7.8:8080",
            "socks4://9.9.9.9:1080",
        ]
    ) == ["http://5.6.7.8:8080"]


def test_validation_candidates_include_socks_when_pysocks_available(monkeypatch) -> None:
    monkeypatch.setattr("kharej.proxy_manager._socks_support_available", lambda: True)

    assert _prepare_validation_candidates(
        [
            "socks5://1.2.3.4:1080",
            "http://5.6.7.8:8080",
            "socks4://9.9.9.9:1080",
        ]
    ) == ["http://5.6.7.8:8080", "socks5://1.2.3.4:1080", "socks4://9.9.9.9:1080"]


def test_refresh_uses_raw_sources_when_pyfreeproxy_sources_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "kharej.proxy_manager._fetch_raw_proxy_lists",
        lambda: ["socks5://10.0.0.4:1081", "http://10.0.0.5:8080"],
    )
    def _fake_validate_raw_sources(candidates, *, on_valid=None):
        results = [(url, 100_000.0) for url in candidates]
        if on_valid is not None:
            for url, speed in results:
                on_valid(url, speed)
        return results

    monkeypatch.setattr(
        "kharej.proxy_manager._validate_proxies",
        _fake_validate_raw_sources,
    )
    mgr = ProxyManager(sources=[], cache_file=tmp_path / "proxies.json")

    mgr._refresh()

    assert mgr.public_working_count() == 2
    assert "socks5://10.0.0.4:1081" in mgr._working
    assert "http://10.0.0.5:8080" in mgr._working


def test_refresh_writes_cache_before_and_during_validation(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "proxies.json"
    observed_cache_states: list[str] = []

    monkeypatch.setattr("kharej.proxy_manager._fetch_raw_proxy_lists", lambda: ["http://10.0.0.5:8080"])

    def _fake_validate(candidates, *, on_valid=None):
        observed_cache_states.append(cache.read_text())
        if on_valid is not None:
            on_valid("http://10.0.0.5:8080", 100_000.0)
            observed_cache_states.append(cache.read_text())
        return [("http://10.0.0.5:8080", 100_000.0)]

    monkeypatch.setattr("kharej.proxy_manager._validate_proxies", _fake_validate)
    mgr = ProxyManager(sources=[], cache_file=cache)

    mgr._refresh()

    assert cache.exists()
    assert _BACKUP_PROXY_URL in observed_cache_states[0]
    assert "http://10.0.0.5:8080" in observed_cache_states[1]


def test_backup_proxy_is_last_resort_when_public_proxies_exist(tmp_path: Path) -> None:
    public_proxy = "http://10.0.0.5:8080"
    mgr = ProxyManager(sources=[], cache_file=tmp_path / "proxies.json")
    with mgr._lock:
        mgr._proxy_records = {
            _BACKUP_PROXY_URL: _ProxyRecord(speed_bps=10_000_000),
            public_proxy: _ProxyRecord(speed_bps=100_000),
        }
        mgr._working = [_BACKUP_PROXY_URL, public_proxy]

    assert mgr.get_proxy() == public_proxy


def test_freeproxy_source_list_includes_recent_http_sources(tmp_path: Path) -> None:
    mgr = ProxyManager(cache_file=tmp_path / "proxies.json")

    assert "GeonixProxiedSession" in mgr._sources
    assert "ProxyVerityProxiedSession" in mgr._sources
    assert "PubProxyProxiedSession" in mgr._sources
    assert "FloppyDataProxiedSession" in mgr._sources
