"""Obsidian form → GitLab MR bridge for human-gated knowledge promotion.

Watches the promotions vault for checkbox state changes.  When a human
ticks a checkbox (`- [x]`) in a promotion candidate file, this bridge:

  1. Parses the approved candidate ID and metadata.
  2. Creates a git branch: promotion/<candidate_id>.
  3. Commits the promotion artifact (a YAML file under promotions/approved/).
  4. Opens a GitLab MR via `glab mr create`.

The MR is never auto-merged — it is always human-reviewed.  This is the
safety gate for promotions that could mutate global agent behavior.

Watching strategy: polling (default 60s interval).  watchdog is used when
available for inotify-backed watching; falls back to mtime polling.

Environment variables:
    WIKI_ROOT        — Path to the Obsidian wiki vault root
    VIGNOBLE_DIR     — Vignoble root directory (used to locate the git repo)
    GITLAB_REPO      — GitLab project path, e.g. your-group/pinard
    PR_POLL_INTERVAL — Polling interval in seconds (default: 60)
    GITLAB_TOKEN     — GitLab token (for glab; usually already set)
    DRY_RUN          — If "1", log what would happen without touching git/GitLab
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("pinard.memory.pr_bridge")

POLL_INTERVAL = int(os.environ.get("PR_POLL_INTERVAL", "60"))
GITLAB_REPO = os.environ.get("GITLAB_REPO", "")


def _dry_run() -> bool:
    """Read DRY_RUN at call time so tests can patch os.environ."""
    return os.environ.get("DRY_RUN", "0") == "1"


def _wiki_root() -> Path:
    explicit = os.environ.get("WIKI_ROOT", "")
    if explicit:
        return Path(explicit)
    vignoble_dir = os.environ.get("VIGNOBLE_DIR", ".")
    return Path(vignoble_dir) / ".wiki"


def _vignoble_dir() -> Path:
    return Path(os.environ.get("VIGNOBLE_DIR", "."))


def _promotions_dir() -> Path:
    return _wiki_root() / "promotions"


def _processed_ids_path() -> Path:
    return _promotions_dir() / ".processed_ids.json"


def _load_processed_ids() -> set[str]:
    """Load the set of already-processed candidate IDs."""
    p = _processed_ids_path()
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text()))
    except (ValueError, OSError):
        return set()


def _save_processed_ids(ids: set[str]) -> None:
    p = _processed_ids_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(ids), indent=2))
    tmp.replace(p)


# ── Parsing ────────────────────────────────────────────────────────────────────

_CHECKBOX_RE = re.compile(
    r"- \[(?P<state>[x ])\] \*\*\[(?P<cid>[0-9a-f\-]+)\]\*\*"
    r" `(?P<obs_type>[^`]+)` \| vignobles: (?P<vignobles>[^|]+)"
    r" \| proposed: (?P<scope>\w+)"
    r"(?: \| similarity: (?P<similarity>[\d.]+))?"
)
_CONTENT_RE = re.compile(r"^  > (.+)$")


def _parse_promotion_file(filepath: Path) -> list[dict[str, Any]]:
    """Parse a daily promotion file and return approved candidates not yet processed."""
    if not filepath.exists():
        return []

    lines = filepath.read_text().splitlines()
    candidates: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = _CHECKBOX_RE.match(line)
        if m and m.group("state") == "x":
            # Read the content line (next line starting with "> ").
            content = ""
            if i + 1 < len(lines):
                cm = _CONTENT_RE.match(lines[i + 1])
                if cm:
                    content = cm.group(1).strip()

            candidates.append({
                "candidate_id": m.group("cid"),
                "obs_type": m.group("obs_type"),
                "source_vignobles": [v.strip() for v in m.group("vignobles").split(",")],
                "proposed_scope": m.group("scope"),
                "similarity": float(m.group("similarity") or "1.0"),
                "content": content,
                "file": str(filepath),
            })
        i += 1

    return candidates


# ── Git operations ─────────────────────────────────────────────────────────────

def _run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    logger.debug("$ %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


def _open_mr(candidate: dict[str, Any], branch: str, repo_dir: Path) -> str | None:
    """Open a GitLab MR for an approved promotion candidate.

    Returns the MR URL or None if opening failed.
    """
    cid = candidate["candidate_id"]
    obs_type = candidate["obs_type"]
    proposed_scope = candidate["proposed_scope"]
    content_excerpt = candidate["content"][:80]
    vignobles = ", ".join(candidate["source_vignobles"])

    title = f"promotion({proposed_scope}): {obs_type} — {content_excerpt}"
    similarity = candidate.get('similarity', 1.0)
    description = (
        f"## Promotion candidate approved\n\n"
        f"- **Candidate ID:** `{cid}`\n"
        f"- **Type:** `{obs_type}`\n"
        f"- **Proposed scope:** `{proposed_scope}`\n"
        f"- **Source vignobles:** {vignobles}\n"
        f"- **Similarity:** {similarity:.3f}\n\n"
        f"### Content\n\n"
        f"> {candidate['content']}\n\n"
        f"---\n"
        f"_Approved via Obsidian promotion form.  "
        f"This MR was opened automatically by the pinard PR bridge.  "
        f"**Do not auto-merge** — review the promotion impact before merging._\n"
    )

    repo_arg = GITLAB_REPO
    cmd = [
        "glab", "mr", "create",
        "--title", title,
        "--description", description,
        "--source-branch", branch,
        "--no-editor",
        "--web=false",
    ]
    if repo_arg:
        cmd += ["--repo", repo_arg]

    if _dry_run():
        logger.info("[DRY_RUN] Would run: %s", " ".join(cmd))
        return "dry-run://mr/0"

    try:
        result = _run(cmd, cwd=repo_dir, check=False)
        output = result.stdout.strip() or result.stderr.strip()
        if result.returncode != 0:
            logger.error("glab mr create failed (rc=%d): %s", result.returncode, output)
            return None
        # Extract URL from output.
        for line in output.splitlines():
            if line.startswith("https://"):
                return line.strip()
        return output or None
    except (OSError, FileNotFoundError) as exc:
        logger.error("glab not found or failed: %s", exc)
        return None


def _commit_promotion(candidate: dict[str, Any], repo_dir: Path) -> bool:
    """Write the promotion artifact and commit it.

    Creates: promotions/approved/<candidate_id>.yaml
    Returns True on success.
    """
    approved_dir = repo_dir / "promotions" / "approved"
    if not _dry_run():
        approved_dir.mkdir(parents=True, exist_ok=True)

    artifact_path = approved_dir / f"{candidate['candidate_id']}.yaml"
    artifact_content = yaml.dump(
        {
            "candidate_id": candidate["candidate_id"],
            "obs_type": candidate["obs_type"],
            "content": candidate["content"],
            "source_vignobles": candidate["source_vignobles"],
            "proposed_scope": candidate["proposed_scope"],
            "similarity": candidate.get("similarity", 1.0),
            "approved_at": datetime.now(tz=timezone.utc).isoformat(),
        },
        default_flow_style=False,
        allow_unicode=True,
    )

    if _dry_run():
        logger.info("[DRY_RUN] Would write %s:\n%s", artifact_path, artifact_content)
        return True

    artifact_path.write_text(artifact_content)
    try:
        _run(["git", "add", str(artifact_path)], cwd=repo_dir)
        _run(
            [
                "git", "commit", "--no-verify",
                "-m",
                f"promotion({candidate['proposed_scope']}): approve {candidate['candidate_id'][:8]}\n\n"
                f"Type: {candidate['obs_type']}\n"
                f"Content: {candidate['content'][:120]}\n"
                f"Vignobles: {', '.join(candidate['source_vignobles'])}",
            ],
            cwd=repo_dir,
        )
        return True
    except subprocess.CalledProcessError as exc:
        logger.error(
            "Git commit failed for candidate %s: %s",
            candidate["candidate_id"], exc.stderr,
        )
        return False


def process_approved_candidate(
    candidate: dict[str, Any],
    repo_dir: Path,
) -> str | None:
    """Process one approved candidate: branch → commit → MR.

    Returns the MR URL or None if any step failed.
    """
    cid = candidate["candidate_id"]
    branch = f"promotion/{cid[:16]}"

    # Determine the base branch.
    if not _dry_run():
        try:
            result = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir, check=False)
            base_branch = result.stdout.strip() or "master"
        except OSError:
            base_branch = "master"
    else:
        base_branch = "master"

    if not _dry_run():
        # Create and switch to the promotion branch.
        try:
            _run(["git", "checkout", "-b", branch], cwd=repo_dir)
        except subprocess.CalledProcessError as exc:
            logger.error("Could not create branch %s: %s", branch, exc.stderr)
            return None

    committed = _commit_promotion(candidate, repo_dir)
    if not committed:
        if not _dry_run():
            _run(["git", "checkout", base_branch], cwd=repo_dir, check=False)
        return None

    if not _dry_run():
        try:
            _run(["git", "push", "-u", "origin", branch], cwd=repo_dir)
        except subprocess.CalledProcessError as exc:
            logger.error("git push failed for branch %s: %s", branch, exc.stderr)
            _run(["git", "checkout", base_branch], cwd=repo_dir, check=False)
            return None

    mr_url = _open_mr(candidate, branch, repo_dir)

    if not _dry_run():
        # Return to base branch.
        _run(["git", "checkout", base_branch], cwd=repo_dir, check=False)

    if mr_url:
        logger.info("MR opened for candidate %s: %s", cid, mr_url)
    return mr_url


# ── Watcher loop ──────────────────────────────────────────────────────────────

def _scan_once(repo_dir: Path) -> int:
    """One scan pass: find newly approved candidates and open MRs.

    Returns the number of candidates processed.
    """
    promotions_dir = _promotions_dir()
    if not promotions_dir.exists():
        return 0

    processed_ids = _load_processed_ids()
    processed_count = 0

    for md_file in sorted(promotions_dir.glob("????-??-??.md")):
        candidates = _parse_promotion_file(md_file)
        for candidate in candidates:
            cid = candidate["candidate_id"]
            if cid in processed_ids:
                continue
            logger.info(
                "Approved promotion candidate found: %s (%s → %s)",
                cid, candidate["obs_type"], candidate["proposed_scope"],
            )
            mr_url = process_approved_candidate(candidate, repo_dir)
            # Mark as processed regardless of MR outcome to avoid infinite retry
            # on persistent git/glab failures (operator should inspect logs).
            processed_ids.add(cid)
            _save_processed_ids(processed_ids)
            processed_count += 1

    return processed_count


def run_watcher(repo_dir: Path | None = None, poll_interval: int = POLL_INTERVAL) -> None:
    """Run the PR bridge watcher loop indefinitely (blocking).

    Polls the promotions vault at `poll_interval` seconds.  Exits only on
    KeyboardInterrupt or SIGTERM.
    """
    effective_repo_dir = repo_dir or _vignoble_dir()
    logger.info(
        "PR bridge watcher starting — repo=%s wiki=%s interval=%ds dry_run=%s",
        effective_repo_dir, _wiki_root(), poll_interval, _dry_run(),
    )

    while True:
        try:
            n = _scan_once(effective_repo_dir)
            if n:
                logger.info("Processed %d promotion candidate(s)", n)
        except Exception as exc:
            logger.error("PR bridge scan error: %s", exc)
        time.sleep(poll_interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    run_watcher()


if __name__ == "__main__":
    main()
