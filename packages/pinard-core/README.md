# pinard-core

Versioned, pip-installable ontology package for the pinard agent system.

Provides the core entity models, edge models, ontology registry, and versioning
primitives used by pinard's memory layer and by domain repos that extend the
ontology (e.g. genomics-workers).

## Install

```bash
pip install pinard-core==1.0.0
```

Pin the version in your `pyproject.toml` or `requirements.txt`:

```toml
# pyproject.toml
dependencies = ["pinard-core==1.0.0"]
```

```text
# requirements.txt
pinard-core==1.0.0
```

## Usage

```python
from pinard_core import (
    OntologyRegistry,
    Task, Step, Verdict, Decision, Gate, Action,
    Diagnosis, LogPattern, EnvironmentCondition, Artifact,
    DependsOn, Produces, Consumes,
    OntologyVersion,
)

registry = OntologyRegistry()

# Compose core ontology (no domain extension)
composed = registry.compose("my-group-id")
print(composed.entity_types)   # 10 core entity classes
print(composed.edge_types)     # 7 core edge classes
print(composed.version.as_stamp())  # {"pinard_core": "1.0.0"}

# Register a domain extension
from pinard_core import CoreEntity
from pydantic import Field

class SlurmJob(CoreEntity):
    role: str = "slurm_job"
    job_id: int = Field(0, description="SLURM job ID")

registry.register_domain(
    group_id="genomics-build",
    entity_types=[SlurmJob],
    domain_name="genomics",
    domain_version="1.0.0",
)

composed = registry.compose("genomics-build")
print(len(composed.entity_types))  # 11 (10 core + 1 domain)
```

## Versioning

`pinard-core` uses independent semver (`pinard-core-vX.Y.Z` git tags), decoupled
from the pinard chart `v*` tags. The version stamped in portable memory subsets
is `composed.version.as_stamp()["pinard_core"]`.

## Dependencies

- `pydantic>=2`

## Development

This package lives at `packages/pinard-core/` inside the pinard monorepo and
co-evolves with the memory layer. Do not edit the shim files in
`services/memory/ontology/` — edit the source of truth in
`packages/pinard-core/pinard_core/`.
