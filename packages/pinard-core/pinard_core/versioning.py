"""Ontology versioning and migration policy.

Tracks the versions of pinard-core and any active domain ontology so that
portable memory subsets can be version-stamped for reproducibility and audit
(see Amendment-01 Decision A7).

Migration policy for v1: stubs only — policy is defined but not yet automated.
Callers are expected to detect version mismatches and surface them for human review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OntologyVersion:
    """Version metadata for a composed ontology (core + optional domain)."""

    core_version: str
    domain_name: Optional[str] = None
    domain_version: Optional[str] = None

    def as_stamp(self) -> dict:
        """Return a serialisable version-stamp dict for embedding in subsets."""
        stamp: dict = {"pinard_core": self.core_version}
        if self.domain_name is not None:
            stamp["domain"] = {
                "name": self.domain_name,
                "version": self.domain_version or "0.0.0",
            }
        return stamp

    def is_compatible_with(self, other: "OntologyVersion") -> bool:
        """Return True if *other* is forward-compatible with this version.

        v1 policy: versions are compatible when their major segment matches.
        Mismatches require human review — this method surfaces the check only,
        no automatic migration is performed.
        """
        return _major(self.core_version) == _major(other.core_version)


def _major(version: str) -> str:
    """Return the major segment of a semver string."""
    return version.split(".")[0]


@dataclass
class MigrationPolicy:
    """Stub migration policy for ontology version changes.

    v1 behaviour: all migrations are no-ops; callers receive a human-readable
    note explaining what changed.  Automated schema migration is deferred.
    """

    from_version: OntologyVersion
    to_version: OntologyVersion

    def check(self) -> "MigrationResult":
        """Evaluate whether a migration is needed and safe."""
        if self.from_version.core_version == self.to_version.core_version:
            return MigrationResult(needed=False, safe=True, notes="No version change.")

        compatible = self.from_version.is_compatible_with(self.to_version)
        if compatible:
            return MigrationResult(
                needed=True,
                safe=True,
                notes=(
                    f"Minor/patch bump "
                    f"({self.from_version.core_version} → {self.to_version.core_version}). "
                    "Additive change — existing subsets remain readable. "
                    "Human review recommended before promoting new types."
                ),
            )
        return MigrationResult(
            needed=True,
            safe=False,
            notes=(
                f"Major version bump "
                f"({self.from_version.core_version} → {self.to_version.core_version}). "
                "Breaking change — subsets must be re-extracted under the new ontology. "
                "Human-gated review required."
            ),
        )

    def apply(self) -> None:  # noqa: D401
        """Apply the migration (v1: no-op — raise if unsafe)."""
        result = self.check()
        if not result.safe:
            raise MigrationError(
                f"Unsafe migration: {result.notes} "
                "Perform manual re-extraction and update the ontology version."
            )
        # Safe migrations: nothing to do in v1.


@dataclass
class MigrationResult:
    needed: bool
    safe: bool
    notes: str


class MigrationError(RuntimeError):
    """Raised when an unsafe migration is attempted."""
