"""Compatibility shim — re-exports from pinard_core."""
from pinard_core.versioning import *  # noqa: F401, F403
from pinard_core.versioning import (
    MigrationError,
    MigrationPolicy,
    MigrationResult,
    OntologyVersion,
)
