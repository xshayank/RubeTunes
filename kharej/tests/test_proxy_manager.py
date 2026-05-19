from __future__ import annotations

import asyncio
import time
from pathlib import Path

from kharej.proxy_manager import (
    ProxyManager,
    _BACKUP_PROXY_URL,
    _ProxyRecord,
    _fetch_proxies_from_source,
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

    mgr.mark_proxy_failed("http://127.0.0.1:8080")

    proxy = asyncio.run(mgr.scan_and_get_proxy())

    assert proxy == _BACKUP_PROXY_URL
    assert mgr.working_count() == 0
    assert mgr.refresh_calls == 0


def test_concurrent_empty_pool_requests_share_one_refill(tmp_path: Path) -> None:
    mgr = _RefillingProxyManager(tmp_path / "proxies.json", delay=0.05)

    async def _run() -> list[str | None]:
        return await asyncio.gather(*(mgr.scan_and_get_proxy() for _ in range(5)))

    proxies = asyncio.run(_run())

    assert proxies == [_BACKUP_PROXY_URL] * 5
    assert mgr.refresh_calls == 0


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


def test_empty_pool_returns_backup_proxy_without_refreshing(tmp_path: Path) -> None:
    mgr = ProxyManager(sources=[], cache_file=tmp_path / "proxies.json")

    proxy = asyncio.run(mgr.scan_and_get_proxy())

    assert proxy == _BACKUP_PROXY_URL


def test_backup_proxy_is_protected_from_eviction(tmp_path: Path) -> None:
    mgr = ProxyManager(sources=[], cache_file=tmp_path / "proxies.json")

    mgr.mark_proxy_failed(_BACKUP_PROXY_URL)

    assert asyncio.run(mgr.scan_and_get_proxy()) == _BACKUP_PROXY_URL


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


def test_freeproxy_source_list_includes_recent_http_sources(tmp_path: Path) -> None:
    mgr = ProxyManager(cache_file=tmp_path / "proxies.json")

    assert "GeonixProxiedSession" in mgr._sources
    assert "ProxyVerityProxiedSession" in mgr._sources
    assert "PubProxyProxiedSession" in mgr._sources
    assert "FloppyDataProxiedSession" in mgr._sources
