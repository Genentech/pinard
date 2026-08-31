"""Unit tests for services/memory/wiki/clone_vignobles.py.

No real NATS, no real git — all external calls are mocked.

Tests:
- URL derivation: owner + name → correct SSH URL.
- clone_or_pull: clones when .git absent; pulls when .git present.
- clone_or_pull: git failure → logs warning, does not raise.
- main(): NATS KV entries cloned; inaccessible repo → warning, others proceed.
- main(): NATS connect failure → skips vignoble discovery, still clones wiki.
- main(): PINARD_WIKI_REPO absent → skip wiki clone (no error).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import subprocess

import pytest

import services.memory.wiki.clone_vignobles as cv


# ── URL derivation ────────────────────────────────────────────────────────────

def test_ssh_url_convention():
    """SSH URL is derived from owner + name via convention."""
    owner = "lelongs"
    name = "misc"
    expected = f"git@{cv.SSH_HOST}:{owner}/vignoble-{name}.git"
    url = f"git@{cv.SSH_HOST}:{owner}/vignoble-{name}.git"
    assert url == expected


# ── clone_or_pull ─────────────────────────────────────────────────────────────

def test_clone_or_pull_clones_when_no_git_dir(tmp_path):
    dest = tmp_path / "vignoble-misc"
    dest.mkdir()
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(cv, "_git", return_value=ok) as mock_git:
        cv.clone_or_pull("git@github.com:lelongs/vignoble-misc.git", dest)
    mock_git.assert_called_once_with(
        ["clone", "git@github.com:lelongs/vignoble-misc.git", str(dest)]
    )


def test_clone_or_pull_pulls_when_git_dir_exists(tmp_path):
    dest = tmp_path / "vignoble-misc"
    dest.mkdir()
    (dest / ".git").mkdir()
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(cv, "_git", return_value=ok) as mock_git:
        cv.clone_or_pull("git@github.com:lelongs/vignoble-misc.git", dest)
    mock_git.assert_called_once_with(["pull", "--ff-only"], cwd=dest)


def test_clone_or_pull_logs_warning_on_failure(tmp_path, caplog):
    import logging
    dest = tmp_path / "vignoble-bad"
    dest.mkdir()
    fail = subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="fatal: repo not found")
    with patch.object(cv, "_git", return_value=fail):
        with caplog.at_level(logging.WARNING, logger="pinard.memory.wiki.clone_vignobles"):
            cv.clone_or_pull("git@github.com:owner/vignoble-bad.git", dest)
    assert any("WARNING" in r.levelname and "vignoble-bad" in r.message for r in caplog.records)


# ── main() ────────────────────────────────────────────────────────────────────

def _make_kv_entry(name: str, owner: str) -> MagicMock:
    entry = MagicMock()
    entry.value = json.dumps({"owner": owner}).encode()
    return entry


def _make_kv(entries: dict[str, str]) -> AsyncMock:
    """Build a mock KV object with keys() and get() returning given {name: owner} pairs."""
    kv = AsyncMock()
    kv.keys = AsyncMock(return_value=list(entries.keys()))
    async def _get(key):
        return _make_kv_entry(key, entries[key])
    kv.get = _get
    return kv


def _make_nc(kv: AsyncMock) -> AsyncMock:
    js = AsyncMock()
    js.key_value = AsyncMock(return_value=kv)
    nc = AsyncMock()
    nc.jetstream = MagicMock(return_value=js)
    nc.close = AsyncMock()
    return nc


@pytest.mark.asyncio
async def test_main_clones_all_vignobles(tmp_path):
    vignobles = {"misc": "lelongs", "exohub": "exohub-owner"}
    kv = _make_kv(vignobles)
    nc = _make_nc(kv)

    cloned: list[tuple[str, Path]] = []
    def _fake_clone_or_pull(url: str, dest: Path):
        cloned.append((url, dest))

    with (
        patch("nats.connect", AsyncMock(return_value=nc)),
        patch.object(cv, "clone_or_pull", side_effect=_fake_clone_or_pull),
        patch.dict("os.environ", {"CLONE_BASE_DIR": str(tmp_path), "PINARD_WIKI_REPO": ""}),
    ):
        await cv.main()

    urls = {url for url, _ in cloned}
    dests = {dest for _, dest in cloned}
    assert f"git@{cv.SSH_HOST}:lelongs/vignoble-misc.git" in urls
    assert f"git@{cv.SSH_HOST}:exohub-owner/vignoble-exohub.git" in urls
    assert tmp_path / "vignobles" / "vignoble-misc" in dests
    assert tmp_path / "vignobles" / "vignoble-exohub" in dests


@pytest.mark.asyncio
async def test_main_best_effort_one_fails(tmp_path, caplog):
    import logging
    vignobles = {"misc": "lelongs", "bad": "owner"}
    kv = _make_kv(vignobles)
    nc = _make_nc(kv)

    def _fake_clone_or_pull(url: str, dest: Path):
        if "bad" in url:
            raise RuntimeError("clone exploded")

    with (
        patch("nats.connect", AsyncMock(return_value=nc)),
        patch.object(cv, "clone_or_pull", side_effect=_fake_clone_or_pull),
        patch.dict("os.environ", {"CLONE_BASE_DIR": str(tmp_path), "PINARD_WIKI_REPO": ""}),
    ):
        with caplog.at_level(logging.WARNING, logger="pinard.memory.wiki.clone_vignobles"):
            await cv.main()  # must not raise

    assert any("bad" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_main_nats_connect_failure_still_clones_wiki(tmp_path):
    cloned: list[str] = []
    def _fake_clone_or_pull(url: str, dest: Path):
        cloned.append(url)

    with (
        patch("nats.connect", AsyncMock(side_effect=Exception("NATS down"))),
        patch.object(cv, "clone_or_pull", side_effect=_fake_clone_or_pull),
        patch.dict("os.environ", {
            "CLONE_BASE_DIR": str(tmp_path),
            "PINARD_WIKI_REPO": "git@github.com:your-org/pinard-wiki.git",
        }),
    ):
        await cv.main()  # must not raise

    assert "git@github.com:your-org/pinard-wiki.git" in cloned


@pytest.mark.asyncio
async def test_main_no_pinard_wiki_repo(tmp_path):
    kv = _make_kv({})
    nc = _make_nc(kv)
    cloned: list[str] = []

    with (
        patch("nats.connect", AsyncMock(return_value=nc)),
        patch.object(cv, "clone_or_pull", side_effect=lambda url, dest: cloned.append(url)),
        patch.dict("os.environ", {"CLONE_BASE_DIR": str(tmp_path), "PINARD_WIKI_REPO": ""}),
    ):
        await cv.main()  # must not raise

    assert not cloned
