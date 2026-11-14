"""STEP 7 · plan B: the same shapes as JSON files under /d/{DATA_PATH}/, pushed to gh-pages only."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import config
import shapes
from publish import GhPagesDeployer, Publisher, SecretLeak
from tests.fakes import MINT, make_ctx
from tests.test_api import seed

DP = "Zq" * 20 + "fakeDataPath"       # 52 chars, test-only


def _pub(tmp_path, ctx, secrets=("fake-app-token-xyz", DP)):
    return Publisher(ctx.conn, site_dir=tmp_path / "site", data_path=DP, secrets=list(secrets))


def test_files_and_shapes_match_api(tmp_path):
    ctx = make_ctx(tmp_path)
    seed(ctx, n_activity=65)
    pub = _pub(tmp_path, ctx)
    pub.copy_web()
    pub.write_all(ctx.now())
    d = tmp_path / "site" / "d" / DP
    names = sorted(str(p.relative_to(d)) for p in d.rglob("*.json"))
    for want in ("state.json", "chart.json", "activity.json", f"coin/{MINT}.json", f"audit/{MINT}.json",
                 "agents/scanner.json", "agents/risk.json", "account.json", "settings.json"):
        assert want in names
    assert len([n for n in names if n.startswith("activity/")]) == 2          # 65 rows = 3 pages
    st = json.loads((d / "state.json").read_text())
    assert st == json.loads(json.dumps(shapes.state(ctx.conn, ctx.now()), default=str))
    ch = json.loads((d / "chart.json").read_text())
    assert set(ch) == {"LIVE", "1H", "4H", "1D", "1W", "ALL"}
    assert json.loads((d / "settings.json").read_text())["readOnly"] is True


def test_no_index_under_d_and_web_at_root(tmp_path):
    ctx = make_ctx(tmp_path)
    seed(ctx)
    pub = _pub(tmp_path, ctx)
    pub.copy_web()
    pub.write_all(ctx.now())
    site = tmp_path / "site"
    assert (site / "index.html").is_file() and (site / ".nojekyll").is_file()
    assert not list((site / "d").rglob("index.html"))                     # bare /d/ and /d/<path>/ = 404
    assert [p.name for p in (site / "d").iterdir()] == [DP]                # nothing else at /d/


def test_data_path_never_inside_any_file(tmp_path):
    ctx = make_ctx(tmp_path)
    seed(ctx)
    pub = _pub(tmp_path, ctx)
    pub.copy_web()
    pub.write_all(ctx.now())
    for p in (tmp_path / "site").rglob("*"):
        if p.is_file():
            assert DP.encode() not in p.read_bytes(), p


def test_secret_in_data_is_refused(tmp_path):
    ctx = make_ctx(tmp_path)
    seed(ctx)
    ctx.conn.execute("UPDATE coins SET name='fake-app-token-xyz'")
    ctx.conn.commit()
    with pytest.raises(SecretLeak):
        _pub(tmp_path, ctx).write_all(ctx.now())


def test_short_data_path_rejected(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(ValueError):
        Publisher(ctx.conn, site_dir=tmp_path / "s", data_path="short", secrets=[])


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def test_deployer_pushes_gh_pages_only_as_one_commit(tmp_path):
    ctx = make_ctx(tmp_path)
    seed(ctx)
    pub = _pub(tmp_path, ctx)
    pub.copy_web()
    pub.write_all(ctx.now())
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    dep = GhPagesDeployer(tmp_path / "site", str(bare), token_name=None, min_interval_s=0)
    sha1 = dep.deploy("data 1", force=True)
    pub.write_all(ctx.now())
    sha2 = dep.deploy("data 2", force=True)
    assert sha1 and sha2 and sha1 != sha2
    heads = _git(bare, "for-each-ref", "--format=%(refname)")
    assert heads == "refs/heads/gh-pages"                                 # main is never touched
    assert _git(bare, "rev-list", "--count", "gh-pages") == "1"            # orphan: no history kept
    files = _git(bare, "ls-tree", "-r", "--name-only", "gh-pages").splitlines()
    assert "index.html" in files and f"d/{DP}/state.json" in files


def test_deployer_refuses_main_and_needs_credential(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        GhPagesDeployer(tmp_path, "x", branch="main")
    monkeypatch.setattr(config, "read_secret", lambda n: None)
    d = GhPagesDeployer(tmp_path, "x")
    assert not d.enabled() and d.deploy(force=True) is None


def test_repo_code_has_no_real_data_path():
    """The real DATA_PATH (compared in code, never printed) is in no repo file."""
    real = config.read_secret("DATA_PATH")
    if not real:
        pytest.skip("no DATA_PATH on this box")
    root = Path(__file__).resolve().parent.parent
    skip = {".venv", "site", "secrets", "__pycache__", ".pytest_cache", "logs"}
    for p in root.rglob("*"):
        if p.is_file() and not (set(p.relative_to(root).parts) & skip):
            assert real.encode() not in p.read_bytes(), p.name
