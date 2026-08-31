"""Obsidian promotion candidate writer for the pinard memory layer.

Writes promotion candidates detected by PromotionCandidateDetector to an
Obsidian-compatible wiki vault as markdown files with YAML frontmatter and
checkbox items.  Each candidate file is idempotent — re-running with the
same candidates updates existing checkboxes only if they have not yet been
checked (approved) by a human.

Output layout::

    {wiki_root}/promotions/
        YYYY-MM-DD.md       — daily file, one checkbox per candidate
        index.md            — running index of all promotion runs

Checkbox format (Obsidian-native)::

    - [ ] **[candidate_id]** `rule` | vignobles: a, b | proposed: vignoble
      > always use fix: or feat: commit prefix
      <!-- conflicts: -->

When a human ticks the checkbox (`- [x]`) the PR bridge picks it up.

Environment variables:
    WIKI_ROOT   — Path to the Obsidian wiki vault root
                  (default: $VIGNOBLE_DIR/.wiki)
    VIGNOBLE_DIR — Vignoble root directory
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .promotion import PromotionCandidate

logger = logging.getLogger("pinard.memory.obsidian_promoter")


def _wiki_root() -> Path:
    explicit = os.environ.get("WIKI_ROOT", "")
    if explicit:
        return Path(explicit)
    vignoble_dir = os.environ.get("VIGNOBLE_DIR", ".")
    return Path(vignoble_dir) / ".wiki"


def _promotions_dir(wiki_root: Path) -> Path:
    d = wiki_root / "promotions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure_obsidian_vault(wiki_root: Path) -> None:
    """Create a minimal .obsidian/ config if the vault doesn't exist yet."""
    obsidian_dir = wiki_root / ".obsidian"
    if obsidian_dir.exists():
        return
    obsidian_dir.mkdir(parents=True, exist_ok=True)
    (obsidian_dir / "app.json").write_text("{}\n")
    logger.info("Initialized Obsidian vault at %s", wiki_root)


def _candidate_checkbox(candidate: PromotionCandidate) -> str:
    """Render a single candidate as an Obsidian checkbox block."""
    vignobles_str = ", ".join(candidate.source_vignobles)
    lines = [
        f"- [ ] **[{candidate.candidate_id}]** `{candidate.obs_type}` "
        f"| vignobles: {vignobles_str} "
        f"| proposed: {candidate.proposed_scope} "
        f"| similarity: {candidate.similarity:.3f}",
        f"  > {candidate.content}",
    ]
    if candidate.conflicts:
        lines.append("  <!-- conflicts:")
        for c in candidate.conflicts:
            lines.append(f"    - {c[:200]}")
        lines.append("  -->")
    return "\n".join(lines)


def _load_existing_checkboxes(filepath: Path) -> dict[str, str]:
    """Parse existing checkbox states from a promotion file.

    Returns {candidate_id: "x" | " "}.
    """
    if not filepath.exists():
        return {}

    pattern = re.compile(r"- \[([x ])\] \*\*\[([0-9a-f\-]+)\]\*\*")
    states: dict[str, str] = {}
    for line in filepath.read_text().splitlines():
        m = pattern.match(line.strip())
        if m:
            state, cid = m.group(1), m.group(2)
            states[cid] = state
    return states


def write_candidates(
    candidates: list[PromotionCandidate],
    wiki_root: Path | None = None,
    run_date: date | None = None,
) -> Path:
    """Write promotion candidates to the Obsidian wiki vault.

    Creates or updates the daily promotions file.  Candidates whose
    checkboxes have already been checked (approved) by a human are preserved
    as-is (not reset to unchecked).

    Returns the path to the written file.
    """
    if not candidates:
        logger.debug("No promotion candidates to write")
        return Path("/dev/null")

    root = wiki_root or _wiki_root()
    _ensure_obsidian_vault(root)
    promotions_dir = _promotions_dir(root)

    today = run_date or date.today()
    filepath = promotions_dir / f"{today.isoformat()}.md"

    # Load existing checkbox states so we don't reset approved items.
    existing_states = _load_existing_checkboxes(filepath)

    # Build frontmatter.
    frontmatter: dict[str, Any] = {
        "type": "promotion-candidates",
        "date": today.isoformat(),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "total_candidates": len(candidates),
    }

    lines = [
        "---",
        yaml.dump(frontmatter, default_flow_style=False).rstrip(),
        "---",
        "",
        f"# Promotion Candidates — {today.isoformat()}",
        "",
        "Review each candidate below.  Tick the checkbox (`- [x]`) to approve "
        "a promotion.  The PR bridge will open a GitLab MR for each approved item.",
        "",
        "> ⚠️ Approving a **global** candidate may mutate the base prompt "
        "and affect every agent.  Review carefully.",
        "",
    ]

    for candidate in candidates:
        checkbox = _candidate_checkbox(candidate)

        # Preserve existing checked state if this candidate was already reviewed.
        if candidate.candidate_id in existing_states:
            existing_state = existing_states[candidate.candidate_id]
            if existing_state == "x":
                # Re-render with checked state.
                checkbox = checkbox.replace("- [ ]", "- [x]", 1)

        lines.append(checkbox)
        lines.append("")

    content = "\n".join(lines)
    filepath.write_text(content)
    logger.info(
        "Wrote %d promotion candidates to %s", len(candidates), filepath
    )

    _update_index(root, promotions_dir, today)
    return filepath


def _update_index(wiki_root: Path, promotions_dir: Path, today: date) -> None:
    """Maintain a running index.md in the promotions directory."""
    index_path = promotions_dir / "index.md"

    # Collect existing entry dates.
    existing_lines: list[str] = []
    if index_path.exists():
        existing_lines = index_path.read_text().splitlines()

    today_entry = f"- [[{today.isoformat()}]] — {today.isoformat()}"
    if not any(today.isoformat() in line for line in existing_lines):
        existing_lines.append(today_entry)

    header = [
        "# Promotion Candidates Index",
        "",
        "Auto-generated.  Each entry links to a daily promotion review file.",
        "",
    ]
    index_path.write_text(
        "\n".join(header + [l for l in existing_lines if l.startswith("- [[")])
        + "\n"
    )
