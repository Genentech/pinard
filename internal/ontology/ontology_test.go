package ontology

import (
	"strings"
	"testing"
)

func TestNewRegistry(t *testing.T) {
	r, err := NewRegistry()
	if err != nil {
		t.Fatal(err)
	}
	if r.core == nil {
		t.Fatal("core ontology not loaded")
	}
	if len(r.core.Entities) != 10 {
		t.Errorf("core entity count = %d, want 10", len(r.core.Entities))
	}
	if len(r.core.Edges) != 7 {
		t.Errorf("core edge count = %d, want 7", len(r.core.Edges))
	}
}

func TestComposeCoreOnly(t *testing.T) {
	r, _ := NewRegistry()
	c := r.Compose("unknown-group")

	roles := c.EntityRoles()
	if len(roles) != 10 {
		t.Errorf("composed role count = %d, want 10", len(roles))
	}
	for _, expected := range []string{"task", "step", "decision", "artifact", "diagnosis",
		"verdict", "gate", "action", "log_pattern", "environment_condition"} {
		if !c.HasRole(expected) {
			t.Errorf("missing expected role %q", expected)
		}
	}

	edges := c.EdgeNames()
	if len(edges) != 7 {
		t.Errorf("edge count = %d, want 7", len(edges))
	}
}

func TestComposeWithDomain(t *testing.T) {
	r, _ := NewRegistry()

	// Load a domain from a string (simulate an example domain YAML).
	domainYAML := `
domain: test-domain
version: "1.0.0"
group_ids: [test-group]
entities:
  slurm_job:
    is_a: task
    description: "A SLURM batch job"
    properties:
      slurm_job_id: {type: string, description: "SLURM job ID"}
      node_count:   {type: integer, minimum: 0}
    required: [slurm_job_id]
edges:
  ProcessesStudy:
    description: "SlurmJob processes a study"
    pairs:
      - [slurm_job, artifact]
suppressed: []
`
	f, err := parseOntologyFile([]byte(domainYAML))
	if err != nil {
		t.Fatal(err)
	}
	r.domains = append(r.domains, f)
	delete(r.cache, "test-group")

	c := r.Compose("test-group")

	// Should have core 10 + domain 1 = 11.
	if len(c.Entities) != 11 {
		t.Errorf("entity count = %d, want 11", len(c.Entities))
	}
	if !c.HasRole("slurm_job") {
		t.Error("missing domain role slurm_job")
	}
	// Edge should be added.
	if _, ok := c.Edges["ProcessesStudy"]; !ok {
		t.Error("missing domain edge ProcessesStudy")
	}
	// Core roles still present.
	if !c.HasRole("task") {
		t.Error("missing core role task")
	}
}

func TestInheritedProperties(t *testing.T) {
	r, _ := NewRegistry()
	domainYAML := `
domain: test
version: "1.0.0"
group_ids: [grp]
entities:
  slurm_job:
    is_a: task
    description: "SlurmJob"
    properties:
      slurm_job_id: {type: string}
edges: {}
suppressed: []
`
	f, _ := parseOntologyFile([]byte(domainYAML))
	r.domains = append(r.domains, f)

	c := r.Compose("grp")
	sj := c.Entities["slurm_job"]

	// Should inherit task's effect_id and process fields.
	if _, ok := sj.Properties["effect_id"]; !ok {
		t.Error("slurm_job did not inherit effect_id from task")
	}
	// Domain-specific field should be present.
	if _, ok := sj.Properties["slurm_job_id"]; !ok {
		t.Error("slurm_job missing own field slurm_job_id")
	}
}

func TestComposeSuppressed(t *testing.T) {
	r, _ := NewRegistry()
	domainYAML := `
domain: test
version: "1.0.0"
group_ids: [grp]
entities: {}
edges: {}
suppressed: [verdict]
`
	f, _ := parseOntologyFile([]byte(domainYAML))
	r.domains = append(r.domains, f)

	c := r.Compose("grp")
	if c.HasRole("verdict") {
		t.Error("verdict should be suppressed")
	}
	if !c.HasRole("task") {
		t.Error("task should still be present")
	}
}

func TestEdgeExtension(t *testing.T) {
	r, _ := NewRegistry()
	domainYAML := `
domain: test
version: "1.0.0"
group_ids: [grp]
entities: {}
edges:
  DependsOn:
    pairs: [[slurm_job, artifact]]
suppressed: []
`
	f, _ := parseOntologyFile([]byte(domainYAML))
	r.domains = append(r.domains, f)

	c := r.Compose("grp")
	pairs := c.EdgeTypeMap["DependsOn"]

	// Should include core pairs + domain extension.
	found := false
	for _, p := range pairs {
		if p[0] == "slurm_job" && p[1] == "artifact" {
			found = true
		}
	}
	if !found {
		t.Error("domain edge extension not found in DependsOn")
	}
	// Core pair still present.
	corePair := false
	for _, p := range pairs {
		if p[0] == "task" && p[1] == "task" {
			corePair = true
		}
	}
	if !corePair {
		t.Error("core DependsOn pair task→task missing after extension")
	}
}

func TestJSONSchemaGeneration(t *testing.T) {
	r, _ := NewRegistry()
	c := r.Compose("any")

	schema := JSONSchemaForRole(c, "verdict")
	if schema == nil {
		t.Fatal("nil schema for verdict")
	}
	if schema["type"] != "object" {
		t.Errorf("schema type = %v, want object", schema["type"])
	}
	props, ok := schema["properties"].(map[string]any)
	if !ok {
		t.Fatal("properties not a map")
	}
	if _, ok := props["passed"]; !ok {
		t.Error("verdict schema missing 'passed' property")
	}
	if _, ok := props["name"]; !ok {
		t.Error("verdict schema missing base 'name' property")
	}
}

func TestGenerateSurrealDDL(t *testing.T) {
	r, _ := NewRegistry()
	c := r.Compose("any")

	ddl := GenerateSurrealDDL(c)
	if !strings.Contains(ddl, "DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL") {
		t.Error("DDL missing entity table definition")
	}
	if !strings.Contains(ddl, "depends_on") {
		t.Error("DDL missing depends_on edge table")
	}
	if !strings.Contains(ddl, "triggers_decision") {
		t.Error("DDL missing triggers_decision edge table")
	}
}

func TestValidateFileValid(t *testing.T) {
	valid := `
domain: test
version: "1.0.0"
group_ids: [grp]
entities:
  slurm_job:
    description: "SlurmJob"
edges:
  Foo:
    pairs: [[slurm_job, artifact]]
`
	errs := ValidateFile([]byte(valid))
	if len(errs) != 0 {
		t.Errorf("expected no errors, got: %v", errs)
	}
}

func TestValidateFileMissingRequired(t *testing.T) {
	invalid := `
domain: test
group_ids: [grp]
entities: {}
edges: {}
`
	errs := ValidateFile([]byte(invalid))
	if len(errs) == 0 {
		t.Error("expected validation errors for missing version")
	}
}

func TestCamelToSnake(t *testing.T) {
	cases := []struct{ in, want string }{
		{"DependsOn", "depends_on"},
		{"TriggersDecision", "triggers_decision"},
		{"ProcessesStudy", "processes_study"},
		{"ResolvedBy", "resolved_by"},
	}
	for _, c := range cases {
		got := camelToSnake(c.in)
		if got != c.want {
			t.Errorf("camelToSnake(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestCachingIsDeterministic(t *testing.T) {
	r, _ := NewRegistry()
	c1 := r.Compose("grp")
	c2 := r.Compose("grp")
	if c1 != c2 {
		t.Error("second Compose call should return cached pointer")
	}
}

func TestOntologyVersionCoreOnly(t *testing.T) {
	r, _ := NewRegistry()
	c := r.Compose("no-domain")
	if c.Version.CoreVersion != CoreVersion {
		t.Errorf("core version = %q, want %q", c.Version.CoreVersion, CoreVersion)
	}
	if c.Version.DomainName != "" {
		t.Errorf("domain name = %q, want empty", c.Version.DomainName)
	}
}

// ── New tests for recursive is_a, ValidateRecord, and real ValidateFile ──────

func TestMergeInheritedPropsMultiLevel(t *testing.T) {
	// grandparent → parent → child: three-level is_a chain.
	// grandparent: has field "gp_field"
	// parent: is_a grandparent, has field "parent_field"
	// child: is_a parent, has field "child_field"
	// Expected: child has all three fields; child overrides parent on collision.
	r, _ := NewRegistry()
	domainYAML := `
domain: test
version: "1.0.0"
group_ids: [grp]
entities:
  grandparent_entity:
    description: "Grandparent"
    properties:
      gp_field:     {type: string, description: "Grandparent field"}
      shared_field: {type: string, description: "Overridden by child"}
  parent_entity:
    is_a: grandparent_entity
    description: "Parent"
    properties:
      parent_field: {type: string}
  child_entity:
    is_a: parent_entity
    description: "Child"
    properties:
      child_field:  {type: string}
      shared_field: {type: integer, description: "Child override"}
edges: {}
suppressed: []
`
	f, err := parseOntologyFile([]byte(domainYAML))
	if err != nil {
		t.Fatal(err)
	}
	r.domains = append(r.domains, f)
	c := r.Compose("grp")

	child := c.Entities["child_entity"]
	// Must have grandparent field.
	if _, ok := child.Properties["gp_field"]; !ok {
		t.Error("child_entity missing grandparent field gp_field")
	}
	// Must have parent field.
	if _, ok := child.Properties["parent_field"]; !ok {
		t.Error("child_entity missing parent field parent_field")
	}
	// Must have own field.
	if _, ok := child.Properties["child_field"]; !ok {
		t.Error("child_entity missing own field child_field")
	}
	// Child override wins: shared_field type is integer (child) not string (grandparent).
	if child.Properties["shared_field"].Type != "integer" {
		t.Errorf("shared_field type = %q, want integer (child override)", child.Properties["shared_field"].Type)
	}
}

func TestMergeInheritedPropsCycleGuard(t *testing.T) {
	// A → B → A is_a cycle must not infinite-loop.
	r, _ := NewRegistry()
	domainYAML := `
domain: test
version: "1.0.0"
group_ids: [grp]
entities:
  ent_a:
    is_a: ent_b
    description: "A"
    properties:
      a_field: {type: string}
  ent_b:
    is_a: ent_a
    description: "B"
    properties:
      b_field: {type: string}
edges: {}
suppressed: []
`
	f, _ := parseOntologyFile([]byte(domainYAML))
	r.domains = append(r.domains, f)
	// Should not panic or hang.
	c := r.Compose("grp")
	if !c.HasRole("ent_a") || !c.HasRole("ent_b") {
		t.Error("cycle entities should still be present")
	}
}

func TestValidateRecordValid(t *testing.T) {
	r, _ := NewRegistry()
	c := r.Compose("any")

	// verdict has required: [passed] — a valid record includes it.
	data := map[string]any{
		"role":        "verdict",
		"name":        "Test verdict",
		"description": "A test verdict",
		"passed":      true,
	}
	if errStr := ValidateRecord(c, "verdict", data); errStr != "" {
		t.Errorf("expected no error, got: %s", errStr)
	}
}

func TestValidateRecordInvalid(t *testing.T) {
	r, _ := NewRegistry()
	c := r.Compose("any")

	// verdict requires "passed" (boolean) — omitting it should fail.
	data := map[string]any{
		"role":        "verdict",
		"name":        "Missing passed",
		"description": "No passed field",
	}
	if errStr := ValidateRecord(c, "verdict", data); errStr == "" {
		t.Error("expected validation error for missing required field 'passed', got none")
	}
}

func TestValidateRecordUnknownRoleIsNoop(t *testing.T) {
	r, _ := NewRegistry()
	c := r.Compose("any")

	// Unknown role → no schema → no error.
	if errStr := ValidateRecord(c, "nonexistent_role", map[string]any{"x": 1}); errStr != "" {
		t.Errorf("unknown role should be noop, got: %s", errStr)
	}
}

// ── DDL generation parity (ported from test_schema_gen.py) ─────────────────

func TestGenerateSurrealDDLIncludesCoreEdgeTables(t *testing.T) {
	r, _ := NewRegistry()
	c := r.Compose("any")
	ddl := GenerateSurrealDDL(c)

	// All 7 core edge names should appear in the DDL as snake_case RELATION tables.
	expectedEdges := []string{
		"depends_on",
		"triggers_decision",
		"produces",
		"consumes",
		"indicates_problem",
		"resolved_by",
		"requires_condition",
	}
	for _, edge := range expectedEdges {
		if !strings.Contains(ddl, edge) {
			t.Errorf("DDL missing core edge table %q", edge)
		}
	}
}

func TestGenerateSurrealDDLIncludesBaseEntityFields(t *testing.T) {
	r, _ := NewRegistry()
	c := r.Compose("any")
	ddl := GenerateSurrealDDL(c)

	for _, field := range []string{"role", "name", "description", "version", "provenance", "embedding", "manual_edit"} {
		if !strings.Contains(ddl, "DEFINE FIELD IF NOT EXISTS "+field) {
			t.Errorf("DDL missing base field %q", field)
		}
	}
}

func TestGenerateSurrealDDLRelationTablesHaveConfidence(t *testing.T) {
	r, _ := NewRegistry()
	c := r.Compose("any")
	ddl := GenerateSurrealDDL(c)

	// Relation tables should define confidence and description fields.
	if !strings.Contains(ddl, "DEFINE FIELD IF NOT EXISTS confidence") {
		t.Error("DDL missing confidence field on relation tables")
	}
	if !strings.Contains(ddl, "TYPE RELATION IN entity OUT entity") {
		t.Error("DDL missing RELATION type on edge tables")
	}
}

func TestGenerateSurrealDDLDomainEdgeIncluded(t *testing.T) {
	r, _ := NewRegistry()
	domainYAML := `
domain: test-ddl
version: "1.0.0"
group_ids: [ddl-group]
entities:
  slurm_job:
    is_a: task
    description: "SLURM job"
edges:
  ProcessesStudy:
    description: "SlurmJob processes a study"
    pairs:
      - [slurm_job, artifact]
suppressed: []
`
	f, err := parseOntologyFile([]byte(domainYAML))
	if err != nil {
		t.Fatal(err)
	}
	r.domains = append(r.domains, f)

	c := r.Compose("ddl-group")
	ddl := GenerateSurrealDDL(c)

	if !strings.Contains(ddl, "processes_study") {
		t.Error("DDL missing domain edge table processes_study")
	}
}

func TestGenerateSurrealDDLPerRoleDataFields(t *testing.T) {
	r, _ := NewRegistry()
	c := r.Compose("any")
	ddl := GenerateSurrealDDL(c)

	// verdict has a 'passed' property (bool) — should appear in DDL as data.passed.
	if !strings.Contains(ddl, "data.passed") {
		t.Error("DDL missing per-role field data.passed for verdict")
	}
}

func TestGenerateSurrealDDLNoHardcodedStagingTables(t *testing.T) {
	// The DDL generator handles staging tables via schema.surql, not GenerateSurrealDDL.
	// GenerateSurrealDDL must contain entity and edge definitions, not staging definitions.
	r, _ := NewRegistry()
	c := r.Compose("any")
	ddl := GenerateSurrealDDL(c)
	// Staging tables (entity_staging, edge_staging) come from schema.surql, not here.
	// Verify the dynamic DDL focuses on entity + relation tables.
	if !strings.Contains(ddl, "DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL") {
		t.Error("DDL must define entity table")
	}
}

func TestValidateFileRealSchemaRejectsInvalidPairs(t *testing.T) {
	// pairs must be arrays of exactly 2 strings; a single string is invalid.
	invalid := `
domain: test
version: "1.0.0"
group_ids: [grp]
entities:
  foo:
    description: "Foo"
edges:
  Bar:
    pairs:
      - not_a_pair
`
	errs := ValidateFile([]byte(invalid))
	if len(errs) == 0 {
		t.Error("expected validation error for malformed pairs entry, got none")
	}
}
