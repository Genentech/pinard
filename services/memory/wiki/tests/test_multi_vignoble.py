"""Unit tests for multi-vignoble iteration in curator, rollup, and sync_in.

No SurrealDB, no network, no LLM — all external calls are mocked.

Tests:
- rollup._load_all_vignoble_memberships() discovers N vignoble dirs with correct membership.
- rollup.ScopeRollupEngine uses multi-vignoble path when vignobles_base_dir is set.
- curator.curate_all_vignobles() calls WikiCurator.curate() once per group_id, best-effort
  (one failure does not block others).
- curator.curate_all_vignobles() handles global_wiki_root separately.
- sync_in.sync_all_vignobles() calls WikiSyncer.pull()+sync_all() per group_id, best-effort.
- sync_in.sync_all_vignobles() handles global_wiki_root separately.
- Both functions return aggregated counts.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_vignoble_dir(tmp_path: Path, name: str, vignes: list[str]) -> Path:
    """Create a fake vignoble clone directory with a vignes.yaml.

    Creates ``wiki/<group_id>/`` subdirectories for each vigne so that
    the per-vigne namespaced paths used by sync_all_vignobles() exist.
    """
    try:
        import yaml
    except ImportError:
        pytest.skip("PyYAML not available")

    vdir = tmp_path / f"vignoble-{name}"
    vdir.mkdir(parents=True)
    data = {"vignes": {g: {"path": f"~/{g}"} for g in vignes}}
    (vdir / "vignes.yaml").write_text(yaml.dump(data))
    wiki_dir = vdir / "wiki"
    wiki_dir.mkdir()
    for group_id in vignes:
        (wiki_dir / group_id).mkdir()
    return vdir


# ---------------------------------------------------------------------------
# rollup._load_all_vignoble_memberships
# ---------------------------------------------------------------------------

class TestLoadAllVignobleMemberships:
    def test_empty_base_dir(self, tmp_path):
        from services.memory.rollup import _load_all_vignoble_memberships
        result = _load_all_vignoble_memberships(str(tmp_path))
        assert result == {}

    def test_nonexistent_base_dir(self, tmp_path):
        from services.memory.rollup import _load_all_vignoble_memberships
        result = _load_all_vignoble_memberships(str(tmp_path / "does-not-exist"))
        assert result == {}

    def test_single_vignoble(self, tmp_path):
        from services.memory.rollup import _load_all_vignoble_memberships
        _make_vignoble_dir(tmp_path, "exohub", ["exo-cli", "pinard"])
        result = _load_all_vignoble_memberships(str(tmp_path))
        assert len(result) == 1
        assert "exohub" in result
        assert set(result["exohub"]) == {"exo-cli", "pinard"}

    def test_two_vignobles(self, tmp_path):
        from services.memory.rollup import _load_all_vignoble_memberships
        _make_vignoble_dir(tmp_path, "exohub", ["exo-cli", "pinard"])
        _make_vignoble_dir(tmp_path, "gwas", ["genomics-build"])
        result = _load_all_vignoble_memberships(str(tmp_path))
        assert len(result) == 2
        # vignoble names derived from subdir names ("vignoble-" prefix stripped)
        assert "exohub" in result
        assert "gwas" in result
        assert set(result["exohub"]) == {"exo-cli", "pinard"}
        assert result["gwas"] == ["genomics-build"]

    def test_dirs_without_vignes_yaml_are_skipped(self, tmp_path):
        from services.memory.rollup import _load_all_vignoble_memberships
        _make_vignoble_dir(tmp_path, "exohub", ["exo-cli"])
        # A dir without vignes.yaml
        (tmp_path / "not-a-vignoble").mkdir()
        result = _load_all_vignoble_memberships(str(tmp_path))
        assert len(result) == 1

    def test_files_in_base_dir_ignored(self, tmp_path):
        from services.memory.rollup import _load_all_vignoble_memberships
        _make_vignoble_dir(tmp_path, "exohub", ["exo-cli"])
        (tmp_path / "some-file.txt").write_text("ignored")
        result = _load_all_vignoble_memberships(str(tmp_path))
        assert len(result) == 1


# ---------------------------------------------------------------------------
# rollup.ScopeRollupEngine multi-vignoble path
# ---------------------------------------------------------------------------

class TestScopeRollupEngineMultiVignoble:
    def test_uses_multi_path_when_base_dir_set(self, tmp_path):
        from services.memory.rollup import ScopeRollupEngine, _load_all_vignoble_memberships
        _make_vignoble_dir(tmp_path, "exohub", ["exo-cli"])

        with patch("services.memory.rollup._load_all_vignoble_memberships") as mock_load, \
             patch("services.memory.rollup._fetch_all_wiki_docs", return_value=[]), \
             patch("services.memory.rollup._fetch_typed_entities", return_value=[]), \
             patch("services.memory.rollup._cleanup_entity_rows"):
            mock_load.return_value = {}
            engine = ScopeRollupEngine(
                vignobles_base_dir=str(tmp_path),
                embed_fn=lambda t: [0.0] * 8,
            )
            engine.run()
            mock_load.assert_called_once_with(str(tmp_path))

    def test_returns_zero_when_base_dir_empty(self, tmp_path):
        from services.memory.rollup import ScopeRollupEngine
        empty_base = tmp_path / "empty_vignobles"
        empty_base.mkdir()

        with patch("services.memory.rollup._load_all_vignoble_memberships") as mock_multi:
            mock_multi.return_value = {}
            engine = ScopeRollupEngine(vignobles_base_dir=str(empty_base))
            counts = engine.run()
            mock_multi.assert_called_once_with(str(empty_base))
            assert counts == {
                "vignoble_promoted": 0,
                "global_promoted": 0,
                "vignoble_wiki_synthesized": 0,
                "global_wiki_synthesized": 0,
            }


# ---------------------------------------------------------------------------
# curator.curate_all_vignobles
# ---------------------------------------------------------------------------

class TestCurateAllVignobles:
    def _make_mock_curator_class(self, counts: dict | None = None, raises: bool = False):
        """Return a patched WikiCurator class."""
        mock_instance = MagicMock()
        if raises:
            mock_instance.curate.side_effect = RuntimeError("boom")
        else:
            mock_instance.curate.return_value = counts or {"synthesized": 1, "skipped": 0, "errors": 0, "mr_opened": 0}
        mock_class = MagicMock(return_value=mock_instance)
        return mock_class, mock_instance

    def test_no_vignoble_dirs_returns_empty(self, tmp_path):
        from services.memory.wiki.curator import curate_all_vignobles
        registry = MagicMock()
        result = curate_all_vignobles(tmp_path, None, embed_fn=lambda x: [], llm_client=MagicMock(), registry=registry)
        assert result == {}

    def test_nonexistent_base_dir(self, tmp_path):
        from services.memory.wiki.curator import curate_all_vignobles
        registry = MagicMock()
        result = curate_all_vignobles(tmp_path / "no-such-dir", None, embed_fn=lambda x: [], llm_client=MagicMock(), registry=registry)
        assert result == {}

    def test_curates_each_group_id(self, tmp_path):
        from services.memory.wiki.curator import curate_all_vignobles
        vdir = _make_vignoble_dir(tmp_path, "exohub", ["exo-cli", "pinard"])
        wiki_dir = vdir / "wiki"

        registry = MagicMock()
        mock_class, mock_instance = self._make_mock_curator_class()

        with patch("services.memory.wiki.curator.WikiCurator", mock_class), \
             patch("services.memory.surrealdb.client.SurrealClient") as mock_surreal:
            mock_surreal.return_value.__enter__ = lambda s: MagicMock()
            mock_surreal.return_value.__exit__ = MagicMock(return_value=False)
            result = curate_all_vignobles(tmp_path, None, embed_fn=lambda x: [], llm_client=MagicMock(), registry=registry)

        # Two group_ids → curator instantiated twice
        assert mock_class.call_count == 2
        # Each curator receives its own per-vigne repo_path (wiki/<group_id>)
        repo_paths = [c.kwargs.get("repo_path") for c in mock_class.call_args_list]
        assert wiki_dir / "exo-cli" in repo_paths
        assert wiki_dir / "pinard" in repo_paths

    def test_failure_in_one_group_does_not_block_others(self, tmp_path):
        from services.memory.wiki.curator import curate_all_vignobles
        _make_vignoble_dir(tmp_path, "exohub", ["exo-cli", "pinard"])

        registry = MagicMock()
        call_count = {"n": 0}

        def _make_curator(**kwargs):
            inst = MagicMock()
            call_count["n"] += 1
            if call_count["n"] == 1:
                inst.curate.side_effect = RuntimeError("first group fails")
            else:
                inst.curate.return_value = {"synthesized": 1, "skipped": 0, "errors": 0, "mr_opened": 0}
            return inst

        with patch("services.memory.wiki.curator.WikiCurator", side_effect=_make_curator), \
             patch("services.memory.surrealdb.client.SurrealClient") as mock_surreal:
            mock_surreal.return_value.__enter__ = lambda s: MagicMock()
            mock_surreal.return_value.__exit__ = MagicMock(return_value=False)
            result = curate_all_vignobles(tmp_path, None, embed_fn=lambda x: [], llm_client=MagicMock(), registry=registry)

        # Both group_ids attempted; one has error count
        vignoble_key = list(result.keys())[0]
        group_results = list(result[vignoble_key].values())
        error_results = [r for r in group_results if r.get("errors", 0) > 0]
        ok_results = [r for r in group_results if r.get("synthesized", 0) > 0]
        assert len(error_results) == 1
        assert len(ok_results) == 1

    def test_global_wiki_root_curated_separately(self, tmp_path):
        from services.memory.wiki.curator import curate_all_vignobles
        _make_vignoble_dir(tmp_path, "exohub", ["exo-cli"])
        global_wiki = tmp_path / "pinard-wiki"
        global_wiki.mkdir()

        registry = MagicMock()
        mock_class, mock_instance = self._make_mock_curator_class()

        with patch("services.memory.wiki.curator.WikiCurator", mock_class), \
             patch("services.memory.surrealdb.client.SurrealClient") as mock_surreal:
            mock_surreal.return_value.__enter__ = lambda s: MagicMock()
            mock_surreal.return_value.__exit__ = MagicMock(return_value=False)
            result = curate_all_vignobles(
                tmp_path, global_wiki,
                embed_fn=lambda x: [], llm_client=MagicMock(), registry=registry,
            )

        # exo-cli + __global__ = 2 curator instantiations
        assert mock_class.call_count == 2
        assert "__global__" in result

    def test_global_wiki_root_missing_skipped_gracefully(self, tmp_path):
        from services.memory.wiki.curator import curate_all_vignobles
        _make_vignoble_dir(tmp_path, "exohub", ["exo-cli"])

        registry = MagicMock()
        mock_class, mock_instance = self._make_mock_curator_class()

        with patch("services.memory.wiki.curator.WikiCurator", mock_class), \
             patch("services.memory.surrealdb.client.SurrealClient") as mock_surreal:
            mock_surreal.return_value.__enter__ = lambda s: MagicMock()
            mock_surreal.return_value.__exit__ = MagicMock(return_value=False)
            result = curate_all_vignobles(
                tmp_path, tmp_path / "does-not-exist",
                embed_fn=lambda x: [], llm_client=MagicMock(), registry=registry,
            )

        assert "__global__" not in result
        assert mock_class.call_count == 1  # only the vignoble group

    def test_ensure_schema_called_in_per_vignoble_and_global_paths(self, tmp_path):
        """ensure_schema must be called for every group_id (per-vignoble and global).

        This is the mocked regression test for issue #145: without the fix,
        wiki_curator_cursor does not exist in schema-less scopes and curator crashes.
        """
        from services.memory.wiki.curator import curate_all_vignobles
        _make_vignoble_dir(tmp_path, "exohub", ["exo-cli"])
        global_wiki = tmp_path / "pinard-wiki"
        global_wiki.mkdir()

        registry = MagicMock()
        mock_class, mock_instance = self._make_mock_curator_class()

        mock_surreal_instance = MagicMock()
        mock_surreal_ctx = MagicMock()
        mock_surreal_ctx.__enter__ = MagicMock(return_value=mock_surreal_instance)
        mock_surreal_ctx.__exit__ = MagicMock(return_value=False)

        with patch("services.memory.wiki.curator.WikiCurator", mock_class), \
             patch("services.memory.surrealdb.client.SurrealClient", return_value=mock_surreal_ctx):
            curate_all_vignobles(
                tmp_path, global_wiki,
                embed_fn=lambda x: [], llm_client=MagicMock(), registry=registry,
            )

        # ensure_schema must have been called once per group_id (exo-cli + global = 2).
        assert mock_surreal_instance.ensure_schema.call_count == 2, (
            f"ensure_schema called {mock_surreal_instance.ensure_schema.call_count} times, expected 2"
        )


# ---------------------------------------------------------------------------
# sync_in.sync_all_vignobles
# ---------------------------------------------------------------------------

class TestSyncAllVignobles:
    def test_no_vignoble_dirs_returns_empty(self, tmp_path):
        from services.memory.wiki.sync_in import sync_all_vignobles
        registry = MagicMock()
        result = sync_all_vignobles(tmp_path, None, embed_fn=lambda x: [], registry=registry)
        assert result == {}

    def test_nonexistent_base_dir(self, tmp_path):
        from services.memory.wiki.sync_in import sync_all_vignobles
        registry = MagicMock()
        result = sync_all_vignobles(tmp_path / "no-such-dir", None, embed_fn=lambda x: [], registry=registry)
        assert result == {}

    def test_syncs_each_group_id(self, tmp_path):
        from services.memory.wiki.sync_in import sync_all_vignobles
        vdir = _make_vignoble_dir(tmp_path, "exohub", ["exo-cli", "pinard"])
        wiki_dir = vdir / "wiki"

        registry = MagicMock()
        mock_instance = MagicMock()
        mock_instance.pull.return_value = None
        mock_instance.sync_all.return_value = {"ingested": 2, "skipped": 0, "errors": 0}
        mock_class = MagicMock(return_value=mock_instance)

        with patch("services.memory.wiki.sync_in.WikiSyncer", mock_class), \
             patch("services.memory.surrealdb.client.SurrealClient") as mock_surreal:
            mock_surreal.return_value.__enter__ = lambda s: MagicMock()
            mock_surreal.return_value.__exit__ = MagicMock(return_value=False)
            result = sync_all_vignobles(tmp_path, None, embed_fn=lambda x: [], registry=registry)

        # Two group_ids → WikiSyncer instantiated twice
        assert mock_class.call_count == 2
        # Each syncer receives its own per-vigne repo_path (wiki/<group_id>)
        repo_paths = [c.kwargs.get("repo_path") for c in mock_class.call_args_list]
        assert wiki_dir / "exo-cli" in repo_paths
        assert wiki_dir / "pinard" in repo_paths

    def test_failure_in_one_group_does_not_block_others(self, tmp_path):
        from services.memory.wiki.sync_in import sync_all_vignobles
        _make_vignoble_dir(tmp_path, "exohub", ["exo-cli", "pinard"])

        registry = MagicMock()
        call_count = {"n": 0}

        def _make_syncer(**kwargs):
            inst = MagicMock()
            call_count["n"] += 1
            if call_count["n"] == 1:
                inst.sync_all.side_effect = RuntimeError("first group fails")
            else:
                inst.sync_all.return_value = {"ingested": 1, "skipped": 0, "errors": 0}
            inst.pull.return_value = None
            return inst

        with patch("services.memory.wiki.sync_in.WikiSyncer", side_effect=_make_syncer), \
             patch("services.memory.surrealdb.client.SurrealClient") as mock_surreal:
            mock_surreal.return_value.__enter__ = lambda s: MagicMock()
            mock_surreal.return_value.__exit__ = MagicMock(return_value=False)
            result = sync_all_vignobles(tmp_path, None, embed_fn=lambda x: [], registry=registry)

        vignoble_key = list(result.keys())[0]
        group_results = list(result[vignoble_key].values())
        error_results = [r for r in group_results if r.get("errors", 0) > 0]
        ok_results = [r for r in group_results if r.get("ingested", 0) > 0]
        assert len(error_results) == 1
        assert len(ok_results) == 1

    def test_global_wiki_root_synced_separately(self, tmp_path):
        from services.memory.wiki.sync_in import sync_all_vignobles
        _make_vignoble_dir(tmp_path, "exohub", ["exo-cli"])
        global_wiki = tmp_path / "pinard-wiki"
        global_wiki.mkdir()

        registry = MagicMock()
        mock_instance = MagicMock()
        mock_instance.pull.return_value = None
        mock_instance.sync_all.return_value = {"ingested": 3, "skipped": 0, "errors": 0}
        mock_class = MagicMock(return_value=mock_instance)

        with patch("services.memory.wiki.sync_in.WikiSyncer", mock_class), \
             patch("services.memory.surrealdb.client.SurrealClient") as mock_surreal:
            mock_surreal.return_value.__enter__ = lambda s: MagicMock()
            mock_surreal.return_value.__exit__ = MagicMock(return_value=False)
            result = sync_all_vignobles(
                tmp_path, global_wiki,
                embed_fn=lambda x: [], registry=registry,
            )

        # exo-cli + __global__ = 2 syncer instantiations
        assert mock_class.call_count == 2
        assert "__global__" in result

    def test_global_wiki_root_missing_skipped_gracefully(self, tmp_path):
        from services.memory.wiki.sync_in import sync_all_vignobles
        _make_vignoble_dir(tmp_path, "exohub", ["exo-cli"])

        registry = MagicMock()
        mock_instance = MagicMock()
        mock_instance.pull.return_value = None
        mock_instance.sync_all.return_value = {"ingested": 1, "skipped": 0, "errors": 0}
        mock_class = MagicMock(return_value=mock_instance)

        with patch("services.memory.wiki.sync_in.WikiSyncer", mock_class), \
             patch("services.memory.surrealdb.client.SurrealClient") as mock_surreal:
            mock_surreal.return_value.__enter__ = lambda s: MagicMock()
            mock_surreal.return_value.__exit__ = MagicMock(return_value=False)
            result = sync_all_vignobles(
                tmp_path, tmp_path / "does-not-exist",
                embed_fn=lambda x: [], registry=registry,
            )

        assert "__global__" not in result
        assert mock_class.call_count == 1

    def test_pull_failure_does_not_abort_sync(self, tmp_path):
        from services.memory.wiki.sync_in import sync_all_vignobles, WikiSyncError
        _make_vignoble_dir(tmp_path, "exohub", ["exo-cli"])

        registry = MagicMock()
        mock_instance = MagicMock()
        mock_instance.pull.side_effect = WikiSyncError("git pull failed")
        mock_instance.sync_all.return_value = {"ingested": 1, "skipped": 0, "errors": 0}
        mock_class = MagicMock(return_value=mock_instance)

        with patch("services.memory.wiki.sync_in.WikiSyncer", mock_class), \
             patch("services.memory.surrealdb.client.SurrealClient") as mock_surreal:
            mock_surreal.return_value.__enter__ = lambda s: MagicMock()
            mock_surreal.return_value.__exit__ = MagicMock(return_value=False)
            result = sync_all_vignobles(tmp_path, None, embed_fn=lambda x: [], registry=registry)

        # sync_all still called despite pull failure
        mock_instance.sync_all.assert_called_once()
        vignoble_key = list(result.keys())[0]
        assert result[vignoble_key]["exo-cli"]["ingested"] == 1


# ---------------------------------------------------------------------------
# Per-vigne namespacing — cross-contamination fix
# ---------------------------------------------------------------------------

class TestPerVigneNamespacing:
    """Verify that each group_id is isolated under wiki/<group_id>/ and not mixed."""

    def test_curator_repo_path_is_per_vigne(self, tmp_path):
        """curate_all_vignobles passes wiki/<group_id> as repo_path, not the shared wiki_dir."""
        from services.memory.wiki.curator import curate_all_vignobles
        vdir = _make_vignoble_dir(tmp_path, "exohub", ["alpha", "beta"])
        wiki_dir = vdir / "wiki"

        captured_repo_paths: list[Path] = []

        def _make_curator(**kwargs):
            captured_repo_paths.append(kwargs.get("repo_path"))
            inst = MagicMock()
            inst.curate.return_value = {"synthesized": 0, "skipped": 0, "errors": 0, "mr_opened": 0}
            return inst

        with patch("services.memory.wiki.curator.WikiCurator", side_effect=_make_curator), \
             patch("services.memory.surrealdb.client.SurrealClient") as mock_surreal:
            mock_surreal.return_value.__enter__ = lambda s: MagicMock()
            mock_surreal.return_value.__exit__ = MagicMock(return_value=False)
            curate_all_vignobles(tmp_path, None, embed_fn=lambda x: [], llm_client=MagicMock(), registry=MagicMock())

        # Each group_id must get its own subdir, not the shared wiki_dir.
        assert wiki_dir / "alpha" in captured_repo_paths
        assert wiki_dir / "beta" in captured_repo_paths
        assert wiki_dir not in captured_repo_paths

    def test_syncer_repo_path_is_per_vigne(self, tmp_path):
        """sync_all_vignobles passes wiki/<group_id> as repo_path, not the shared wiki_dir."""
        from services.memory.wiki.sync_in import sync_all_vignobles
        vdir = _make_vignoble_dir(tmp_path, "exohub", ["alpha", "beta"])
        wiki_dir = vdir / "wiki"

        captured_repo_paths: list[Path] = []

        def _make_syncer(**kwargs):
            captured_repo_paths.append(kwargs.get("repo_path"))
            inst = MagicMock()
            inst.pull.return_value = None
            inst.sync_all.return_value = {"ingested": 0, "skipped": 0, "errors": 0}
            return inst

        with patch("services.memory.wiki.sync_in.WikiSyncer", side_effect=_make_syncer), \
             patch("services.memory.surrealdb.client.SurrealClient") as mock_surreal:
            mock_surreal.return_value.__enter__ = lambda s: MagicMock()
            mock_surreal.return_value.__exit__ = MagicMock(return_value=False)
            sync_all_vignobles(tmp_path, None, embed_fn=lambda x: [], registry=MagicMock())

        assert wiki_dir / "alpha" in captured_repo_paths
        assert wiki_dir / "beta" in captured_repo_paths
        assert wiki_dir not in captured_repo_paths

    def test_missing_group_wiki_dir_skips_that_group_only(self, tmp_path):
        """If wiki/<group_id>/ does not exist, only that group is skipped (others proceed)."""
        from services.memory.wiki.sync_in import sync_all_vignobles
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML not available")

        vdir = tmp_path / "vignoble-exohub"
        vdir.mkdir()
        data = {"vignes": {"alpha": {"path": "~/alpha"}, "beta": {"path": "~/beta"}}}
        (vdir / "vignes.yaml").write_text(yaml.dump(data))
        wiki_dir = vdir / "wiki"
        wiki_dir.mkdir()
        # Only create wiki/alpha, not wiki/beta
        (wiki_dir / "alpha").mkdir()

        mock_instance = MagicMock()
        mock_instance.pull.return_value = None
        mock_instance.sync_all.return_value = {"ingested": 1, "skipped": 0, "errors": 0}
        mock_class = MagicMock(return_value=mock_instance)

        with patch("services.memory.wiki.sync_in.WikiSyncer", mock_class), \
             patch("services.memory.surrealdb.client.SurrealClient") as mock_surreal:
            mock_surreal.return_value.__enter__ = lambda s: MagicMock()
            mock_surreal.return_value.__exit__ = MagicMock(return_value=False)
            result = sync_all_vignobles(tmp_path, None, embed_fn=lambda x: [], registry=MagicMock())

        # Only the group with an existing wiki/<group_id>/ dir is synced.
        assert mock_class.call_count == 1
        vignoble_result = result.get("vignoble-exohub", {})
        # alpha was synced, beta was skipped with zero counts
        assert vignoble_result.get("alpha", {}).get("ingested") == 1
        assert vignoble_result.get("beta", {}).get("ingested") == 0


# ---------------------------------------------------------------------------
# sync_out_vignoble_shared
# ---------------------------------------------------------------------------

class TestSyncOutVignobleShared:
    """Unit tests for sync_out_vignoble_shared() — vignoble-scope wiki_doc git sync-out."""

    def _wiki_doc(self, path: str, title: str = "Test", role: str = "decision",
                  source: str = "rollup-curator", summary: str = "") -> dict:
        return {
            "path": path,
            "title": title,
            "type": role,
            "summary": summary,
            "body": f"# {title}\n\nBody text.",
            "frontmatter": {"source": source, "type": role, "title": title},
            "confidence": 0.75,
            "embedding": None,
        }

    def test_nonexistent_base_dir_returns_empty(self, tmp_path):
        from services.memory.wiki.curator import sync_out_vignoble_shared
        result = sync_out_vignoble_shared(tmp_path / "no-such-dir", embed_fn=lambda x: [])
        assert result == {}

    def test_writes_auto_serve_docs_to_shared_dir(self, tmp_path):
        from services.memory.wiki.curator import sync_out_vignoble_shared
        vdir = _make_vignoble_dir(tmp_path, "exohub", ["pinard"])
        wiki_dir = vdir / "wiki"

        docs = [self._wiki_doc("decisions/deploy", title="Deploy Strategy")]

        mock_surreal = MagicMock()
        mock_surreal.query.return_value = [docs]
        mock_surreal_ctx = MagicMock()
        mock_surreal_ctx.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal_ctx.__exit__ = MagicMock(return_value=False)

        with patch("services.memory.surrealdb.client.SurrealClient", return_value=mock_surreal_ctx), \
             patch("services.memory.wiki.curator.WikiCurator._commit_and_push_branch", return_value=None):
            result = sync_out_vignoble_shared(tmp_path, embed_fn=lambda x: [], dry_run=True)

        shared_file = wiki_dir / "_shared" / "decisions" / "deploy.md"
        assert shared_file.exists(), f"Expected {shared_file} to be written"
        content = shared_file.read_text()
        assert "Deploy Strategy" in content
        vignoble_key = "vignoble-exohub"
        assert result[vignoble_key]["written"] == 1

    def test_summary_emitted_in_frontmatter(self, tmp_path):
        """summary field from wiki_doc row must appear in the written .md frontmatter."""
        from services.memory.wiki.curator import sync_out_vignoble_shared
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML not available")

        vdir = _make_vignoble_dir(tmp_path, "exohub", ["pinard"])
        wiki_dir = vdir / "wiki"

        docs = [self._wiki_doc(
            "decisions/deploy",
            title="Deploy Strategy",
            summary="Cross-project deployment strategy for all vignes.",
        )]

        mock_surreal = MagicMock()
        mock_surreal.query.return_value = [docs]
        mock_surreal_ctx = MagicMock()
        mock_surreal_ctx.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal_ctx.__exit__ = MagicMock(return_value=False)

        with patch("services.memory.surrealdb.client.SurrealClient", return_value=mock_surreal_ctx), \
             patch("services.memory.wiki.curator.WikiCurator._commit_and_push_branch", return_value=None):
            result = sync_out_vignoble_shared(tmp_path, embed_fn=lambda x: [], dry_run=True)

        shared_file = wiki_dir / "_shared" / "decisions" / "deploy.md"
        assert shared_file.exists()
        content = shared_file.read_text()
        # Frontmatter must carry the intentional summary
        assert "summary:" in content
        assert "Cross-project deployment strategy" in content
        vignoble_key = "vignoble-exohub"
        assert result[vignoble_key]["written"] == 1

    def test_commit_and_push_branch_is_reached(self, tmp_path):
        """_commit_and_push_branch must be called with the written _shared/ paths.

        Regression guard: WikiCurator.__init__ with composed=None previously crashed
        with AttributeError before _commit_and_push_branch ever ran, so the git/MR
        step was silently inert.
        """
        from services.memory.wiki.curator import sync_out_vignoble_shared

        vdir = _make_vignoble_dir(tmp_path, "exohub", ["pinard"])
        wiki_dir = vdir / "wiki"

        docs = [self._wiki_doc("decisions/deploy", title="Deploy Strategy")]

        mock_surreal = MagicMock()
        mock_surreal.query.return_value = [docs]
        mock_surreal_ctx = MagicMock()
        mock_surreal_ctx.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal_ctx.__exit__ = MagicMock(return_value=False)

        push_calls: list = []

        def _fake_commit_and_push(paths):
            push_calls.append(paths)
            return None

        with patch("services.memory.surrealdb.client.SurrealClient", return_value=mock_surreal_ctx), \
             patch(
                 "services.memory.wiki.curator.WikiCurator._commit_and_push_branch",
                 side_effect=_fake_commit_and_push,
             ):
            result = sync_out_vignoble_shared(tmp_path, embed_fn=lambda x: [], dry_run=True)

        vignoble_key = "vignoble-exohub"
        assert result[vignoble_key]["written"] == 1
        assert push_calls, "_commit_and_push_branch was never called — git/MR step silently skipped"
        pushed_paths = push_calls[0]
        assert any("decisions/deploy" in p for p in pushed_paths), (
            f"Expected 'decisions/deploy' in pushed paths, got: {pushed_paths}"
        )

    def test_skips_human_authored_docs(self, tmp_path):
        from services.memory.wiki.curator import sync_out_vignoble_shared
        _make_vignoble_dir(tmp_path, "exohub", ["pinard"])

        docs = [self._wiki_doc("decisions/deploy", source="human")]

        mock_surreal = MagicMock()
        mock_surreal.query.return_value = [docs]
        mock_surreal_ctx = MagicMock()
        mock_surreal_ctx.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal_ctx.__exit__ = MagicMock(return_value=False)

        with patch("services.memory.surrealdb.client.SurrealClient", return_value=mock_surreal_ctx), \
             patch("services.memory.wiki.curator.WikiCurator._commit_and_push_branch", return_value=None):
            result = sync_out_vignoble_shared(tmp_path, embed_fn=lambda x: [], dry_run=True)

        vignoble_key = "vignoble-exohub"
        assert result[vignoble_key]["written"] == 0
        assert result[vignoble_key]["skipped"] == 1

    def test_skips_existing_human_authored_file_on_disk(self, tmp_path):
        """Even if SurrealDB says non-human, a human-edited file on disk is never overwritten."""
        from services.memory.wiki.curator import sync_out_vignoble_shared
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML not available")

        vdir = _make_vignoble_dir(tmp_path, "exohub", ["pinard"])
        wiki_dir = vdir / "wiki"
        shared_file = wiki_dir / "_shared" / "decisions" / "deploy.md"
        shared_file.parent.mkdir(parents=True)
        shared_file.write_text("---\nsource: human\ntitle: Deploy Strategy\n---\nHuman edit.", encoding="utf-8")

        docs = [self._wiki_doc("decisions/deploy", source="rollup-curator")]

        mock_surreal = MagicMock()
        mock_surreal.query.return_value = [docs]
        mock_surreal_ctx = MagicMock()
        mock_surreal_ctx.__enter__ = MagicMock(return_value=mock_surreal)
        mock_surreal_ctx.__exit__ = MagicMock(return_value=False)

        with patch("services.memory.surrealdb.client.SurrealClient", return_value=mock_surreal_ctx), \
             patch("services.memory.wiki.curator.WikiCurator._commit_and_push_branch", return_value=None):
            result = sync_out_vignoble_shared(tmp_path, embed_fn=lambda x: [], dry_run=True)

        vignoble_key = "vignoble-exohub"
        assert result[vignoble_key]["skipped"] == 1
        # Disk file must not have been overwritten.
        assert "Human edit." in shared_file.read_text()

    def test_no_vignoble_dirs_returns_empty(self, tmp_path):
        from services.memory.wiki.curator import sync_out_vignoble_shared
        result = sync_out_vignoble_shared(tmp_path, embed_fn=lambda x: [])
        assert result == {}

    def test_surreal_error_does_not_abort_other_vignobles(self, tmp_path):
        from services.memory.wiki.curator import sync_out_vignoble_shared
        _make_vignoble_dir(tmp_path, "exohub", ["pinard"])
        _make_vignoble_dir(tmp_path, "gwas", ["genomics"])

        call_count = {"n": 0}

        def _make_client(**kwargs):
            ctx = MagicMock()
            call_count["n"] += 1
            if call_count["n"] == 1:
                ctx.__enter__ = MagicMock(side_effect=RuntimeError("db error"))
            else:
                surreal = MagicMock()
                surreal.query.return_value = [[]]
                ctx.__enter__ = MagicMock(return_value=surreal)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("services.memory.surrealdb.client.SurrealClient", side_effect=_make_client):
            result = sync_out_vignoble_shared(tmp_path, embed_fn=lambda x: [], dry_run=True)

        # Both vignobles attempted; the errored one has error count, the other zero written.
        assert len(result) == 2
        error_vignobles = [v for v, c in result.items() if c.get("errors", 0) > 0]
        ok_vignobles = [v for v, c in result.items() if c.get("errors", 0) == 0]
        assert len(error_vignobles) == 1
        assert len(ok_vignobles) == 1


# ---------------------------------------------------------------------------
# Ingester loop wiring — sync_out_vignoble_shared is called after curate_all_vignobles
# ---------------------------------------------------------------------------

class TestIngesterLoopWiring:
    """Verify that sync_out_vignoble_shared is wired into the wiki curator loop
    in ingester.py and called after curate_all_vignobles.

    Reads ingester.py source directly (no import) to avoid the nats/surrealdb
    dependency chain that is unavailable in the unit-test environment.
    """

    @staticmethod
    def _ingester_src() -> str:
        ingester_path = (
            Path(__file__).parent.parent.parent / "ingester.py"
        )
        return ingester_path.read_text(encoding="utf-8")

    def test_sync_out_vignoble_shared_called_in_wiki_loop(self):
        """ingester.py must import and call sync_out_vignoble_shared."""
        src = self._ingester_src()

        assert "sync_out_vignoble_shared" in src, (
            "ingester.py must import sync_out_vignoble_shared from wiki.curator"
        )
        call_sites = src.count("sync_out_vignoble_shared(")
        assert call_sites >= 1, (
            f"sync_out_vignoble_shared must be called in ingester.py; found {call_sites} call-site(s)"
        )

    def test_sync_out_called_after_curate_in_loop_order(self):
        """sync_out_vignoble_shared must appear after curate_all_vignobles in the loop."""
        src = self._ingester_src()
        curate_pos = src.find("curate_all_vignobles(")
        sync_out_pos = src.find("sync_out_vignoble_shared(")

        assert curate_pos != -1, "curate_all_vignobles must be called in ingester"
        assert sync_out_pos != -1, "sync_out_vignoble_shared must be called in ingester"
        assert sync_out_pos > curate_pos, (
            "sync_out_vignoble_shared must appear after curate_all_vignobles in ingester.py "
            "(curate produces _vignoble_db rows; shared sync-out must run after)"
        )
