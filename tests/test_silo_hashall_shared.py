from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import qbit_hashall_shared  # noqa: E402


def test_resolve_hashall_script_from_env(monkeypatch, tmp_path):
    fake_root = tmp_path / "hashall"
    fake_bin = fake_root / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "qb-cache-agent.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (fake_bin / "qb-cache-daemon.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    monkeypatch.setenv("HASHALL_ROOT", str(fake_root))

    resolved = qbit_hashall_shared.resolve_hashall_script("qb-cache-agent.py")
    assert resolved == fake_bin / "qb-cache-agent.py"


def test_default_hashall_cache_base_points_to_shared_cache():
    assert qbit_hashall_shared.DEFAULT_HASHALL_CACHE_BASE == Path.home() / ".cache" / "hashall-qb"
