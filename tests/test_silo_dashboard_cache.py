from pathlib import Path
import importlib.util
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

DASHBOARD_PATH = ROOT / "bin" / "silo-dashboard.py"

spec = importlib.util.spec_from_file_location("qbit_dashboard", DASHBOARD_PATH)
qbit_dashboard = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(qbit_dashboard)


class DummyResult:
    def __init__(self, *, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_fetch_torrents_info_payload_uses_cache_when_available(monkeypatch):
    monkeypatch.setattr(
        qbit_dashboard.subprocess,
        "run",
        lambda *args, **kwargs: DummyResult(returncode=0, stdout='[{"hash":"abc"}]'),
    )

    raw, used_cache, used_direct, error = qbit_dashboard.fetch_torrents_info_payload(
        use_shared_cache=True,
        cache_agent_cmd=Path("/tmp/qbit-cache-agent.py"),
        cache_max_age=15.0,
        cache_wait_fresh=5.0,
        cache_allow_stale=True,
        cache_env={},
        opener=None,
        api_url="http://localhost:9003",
    )

    assert raw == '[{"hash":"abc"}]'
    assert used_cache is True
    assert used_direct is False
    assert error is None


def test_fetch_torrents_info_payload_does_not_fallback_direct_on_cache_failure(monkeypatch):
    monkeypatch.setattr(
        qbit_dashboard.subprocess,
        "run",
        lambda *args, **kwargs: DummyResult(returncode=1, stderr="cache too stale"),
    )

    raw, used_cache, used_direct, error = qbit_dashboard.fetch_torrents_info_payload(
        use_shared_cache=True,
        cache_agent_cmd=Path("/tmp/qbit-cache-agent.py"),
        cache_max_age=15.0,
        cache_wait_fresh=5.0,
        cache_allow_stale=True,
        cache_env={},
        opener=None,
        api_url="http://localhost:9003",
    )

    assert raw == ""
    assert used_cache is False
    assert used_direct is False
    assert "Cache agent failed" in error


def test_fetch_torrents_info_payload_uses_direct_mode_only_when_explicit(monkeypatch):
    monkeypatch.setattr(
        qbit_dashboard,
        "qbit_request",
        lambda opener, api_url, method, path, params=None: '[{"hash":"direct"}]',
    )

    raw, used_cache, used_direct, error = qbit_dashboard.fetch_torrents_info_payload(
        use_shared_cache=False,
        cache_agent_cmd=Path("/tmp/qbit-cache-agent.py"),
        cache_max_age=15.0,
        cache_wait_fresh=5.0,
        cache_allow_stale=True,
        cache_env={},
        opener=None,
        api_url="http://localhost:9003",
    )

    assert raw == '[{"hash":"direct"}]'
    assert used_cache is False
    assert used_direct is True
    assert error is None
