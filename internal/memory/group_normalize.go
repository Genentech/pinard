package memory

import "strings"

// NormalizeGroupID strips the "vignoble-" prefix from a project name to
// produce the canonical group_id used for the SurrealDB database, NATS
// subjects, and ontology scoping.
//
// Examples:
//
//	"vignoble-foo"  → "foo"
//	"foo"           → "foo"
//	"vignoble-bar"  → "bar"
//
// This is an interim normalization (see #240 for the long-term design).
func NormalizeGroupID(project string) string {
	return strings.TrimPrefix(project, "vignoble-")
}

// GroupByCanonical partitions a list of project names by their canonical
// group_id (after NormalizeGroupID). Each canonical id maps to the list of
// source project names that should be ingested into it.
//
// Example:
//
//	["misc","vignoble-misc","pinard"] → {"misc":["misc","vignoble-misc"],"pinard":["pinard"]}
func GroupByCanonical(projects []string) map[string][]string {
	result := make(map[string][]string)
	for _, p := range projects {
		canonical := NormalizeGroupID(p)
		result[canonical] = append(result[canonical], p)
	}
	return result
}
