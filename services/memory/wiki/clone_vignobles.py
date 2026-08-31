"""Clone all vignoble repos discovered via NATS KV pinard-vignobles.

Reads the ``pinard-vignobles`` KV bucket, derives the SSH URL for each
vignoble, and clones it to ``/data/repos/vignobles/vignoble-<name>/``
(or ``git pull --ff-only`` if already present).  Also clones the global
pinard-wiki repo (``PINARD_WIKI_REPO``) to ``/data/repos/pinard-wiki/``.

Every per-repo failure is a WARNING — the script always exits 0 so the pod
init-container never crashes on a missing/inaccessible repo.

Environment variables::

    NATS_URL         — NATS server URL (default: nats://localhost:4222)
    NATS_VIGNOBLE    — Vignoble name (used only for logging; not required)
    CLONE_BASE_DIR   — Parent directory for clones (default: /data/repos)
    PINARD_WIKI_REPO — SSH URL for the global pinard-wiki repo (optional)
    GITLAB_SSH_HOST  — SSH hostname for git clones (default: github.com)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import nats
import nats.js

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("pinard.memory.wiki.clone_vignobles")

KV_BUCKET = "pinard-vignobles"
SSH_HOST = os.environ.get("GITLAB_SSH_HOST", "github.com")


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )


def clone_or_pull(repo_url: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if (dest / ".git").exists():
        logger.info("Pulling %s", dest)
        r = _git(["pull", "--ff-only"], cwd=dest)
        if r.returncode != 0:
            logger.warning("git pull --ff-only failed for %s: %s", dest, r.stderr.strip())
    else:
        logger.info("Cloning %s → %s", repo_url, dest)
        r = _git(["clone", repo_url, str(dest)])
        if r.returncode != 0:
            logger.warning("git clone failed for %s: %s", repo_url, r.stderr.strip())


async def list_vignobles(nc: nats.NATS) -> list[dict]:
    js = nc.jetstream()
    try:
        kv = await js.key_value(KV_BUCKET)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cannot open KV bucket %s: %s", KV_BUCKET, exc)
        return []

    results: list[dict] = []
    try:
        entries = await kv.keys()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cannot list keys in %s: %s", KV_BUCKET, exc)
        return []

    for key in entries:
        try:
            entry = await kv.get(key)
            if entry.value is None:
                continue
            data = json.loads(entry.value.decode())
            owner = data.get("owner", "")
            if not owner:
                logger.warning("No owner for vignoble %s — skipping", key)
                continue
            results.append({"name": key, "owner": owner})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read KV entry %s: %s", key, exc)

    return results


async def main() -> None:
    nats_url = os.environ.get("NATS_URL", "nats://localhost:4222")
    clone_base_dir = Path(os.environ.get("CLONE_BASE_DIR", "/data/repos"))
    pinard_wiki_repo = os.environ.get("PINARD_WIKI_REPO", "")

    logger.info("Connecting to NATS %s", nats_url)
    try:
        nc = await nats.connect(nats_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cannot connect to NATS (%s): %s — skipping vignoble discovery", nats_url, exc)
        vignobles: list[dict] = []
    else:
        vignobles = await list_vignobles(nc)
        await nc.close()

    vignobles_dir = clone_base_dir / "vignobles"
    vignobles_dir.mkdir(parents=True, exist_ok=True)

    for v in vignobles:
        name = v["name"]
        owner = v["owner"]
        repo_url = f"git@{SSH_HOST}:{owner}/vignoble-{name}.git"
        dest = vignobles_dir / f"vignoble-{name}"
        try:
            clone_or_pull(repo_url, dest)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error cloning vignoble %s: %s", name, exc)

    if pinard_wiki_repo:
        wiki_dest = clone_base_dir / "pinard-wiki"
        try:
            clone_or_pull(pinard_wiki_repo, wiki_dest)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error cloning pinard-wiki: %s", exc)
    else:
        logger.info("PINARD_WIKI_REPO not set — skipping pinard-wiki clone")

    logger.info("Done")


if __name__ == "__main__":
    asyncio.run(main())
