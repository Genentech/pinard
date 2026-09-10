// Package ontology implements the pinard declarative ontology registry.
//
// The core ontology (10 entities / 7 edges) is embedded as core.yaml.
// Domain extensions are loaded at runtime from YAML/JSON files in directories
// listed in PINARD_ONTOLOGY_DIRS (colon-separated) and/or
// <vignoble>/pinard/ontology/*.{yaml,yml,json}.
//
// Composition:  compose(group_id) = core + domain(group_id) − suppressed.
//
// The composed result drives:
//   - Ingester record validation via JSON Schema (santhosh-tekuri/jsonschema).
//   - SurrealDB DDL generation (replaces schema_gen.py).
//   - Ontology gardener entity/edge staging decisions.
package ontology

import (
	"bytes"
	"embed"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"

	jsonschema "github.com/santhosh-tekuri/jsonschema/v6"
	"gopkg.in/yaml.v3"
)

//go:embed core.yaml meta-schema.json
var embeddedFS embed.FS

const CoreVersion = "1.0.0"

// ── Data types ────────────────────────────────────────────────────────────────

// PropertySchema is a literal JSON Schema property definition.
type PropertySchema struct {
	Type        string          `yaml:"type"        json:"type"`
	Description string          `yaml:"description" json:"description,omitempty"`
	Format      string          `yaml:"format"      json:"format,omitempty"`
	Minimum     *float64        `yaml:"minimum"     json:"minimum,omitempty"`
	Maximum     *float64        `yaml:"maximum"     json:"maximum,omitempty"`
	Enum        []any           `yaml:"enum"        json:"enum,omitempty"`
	Items       *PropertySchema `yaml:"items"       json:"items,omitempty"`
}

// EntityDef defines one entity role in the ontology.
type EntityDef struct {
	IsA         string                    `yaml:"is_a"        json:"is_a,omitempty"`
	Description string                    `yaml:"description" json:"description"`
	Properties  map[string]PropertySchema `yaml:"properties"  json:"properties,omitempty"`
	Required    []string                  `yaml:"required"    json:"required,omitempty"`
}

// EdgeDef defines one edge type in the ontology.
type EdgeDef struct {
	Description string     `yaml:"description" json:"description,omitempty"`
	Pairs       [][2]string `yaml:"-"           json:"-"`
}

// OntologyFile is the top-level structure of a .yaml ontology file.
type OntologyFile struct {
	Domain    string                `yaml:"domain"`
	Version   string                `yaml:"version"`
	GroupIDs  []string              `yaml:"group_ids"`
	Entities  map[string]EntityDef  `yaml:"entities"`
	Edges     map[string]RawEdgeDef `yaml:"edges"`
	Suppressed []string             `yaml:"suppressed"`
}

// RawEdgeDef is used during YAML parsing (pairs as [][]string before conversion).
type RawEdgeDef struct {
	Description string     `yaml:"description"`
	Pairs       [][]string `yaml:"pairs"`
}

// OntologyVersion is the version stamp for a composed ontology.
type OntologyVersion struct {
	CoreVersion   string
	DomainName    string
	DomainVersion string
}

// ComposedOntology is the result of core + domain − suppressed for a group_id.
type ComposedOntology struct {
	Version     OntologyVersion
	Entities    map[string]EntityDef    // role → def (with inherited properties merged in)
	Edges       map[string]EdgeDef      // name → def
	EdgeTypeMap map[string][][2]string  // edge name → valid (source, target) pairs
}

// EntityRoles returns the list of active entity role strings.
func (c *ComposedOntology) EntityRoles() []string {
	roles := make([]string, 0, len(c.Entities))
	for role := range c.Entities {
		roles = append(roles, role)
	}
	return roles
}

// EdgeNames returns the list of active edge name strings.
func (c *ComposedOntology) EdgeNames() []string {
	names := make([]string, 0, len(c.Edges))
	for name := range c.Edges {
		names = append(names, name)
	}
	return names
}

// HasRole returns true when the role is active in this composition.
func (c *ComposedOntology) HasRole(role string) bool {
	_, ok := c.Entities[role]
	return ok
}

// ── Registry ──────────────────────────────────────────────────────────────────

// Registry manages core + domain ontologies and composes them per group_id.
type Registry struct {
	mu      sync.RWMutex
	core    *OntologyFile
	domains []*OntologyFile // all loaded domain files
	cache   map[string]*ComposedOntology
}

// NewRegistry creates a Registry with the embedded core ontology loaded.
func NewRegistry() (*Registry, error) {
	coreData, err := embeddedFS.ReadFile("core.yaml")
	if err != nil {
		return nil, fmt.Errorf("read embedded core.yaml: %w", err)
	}
	core, err := parseOntologyFile(coreData)
	if err != nil {
		return nil, fmt.Errorf("parse core.yaml: %w", err)
	}
	return &Registry{
		core:  core,
		cache: make(map[string]*ComposedOntology),
	}, nil
}

// LoadDirs loads all *.yaml / *.yml / *.json ontology files from the given
// directories. Errors on individual files are logged but non-fatal.
func (r *Registry) LoadDirs(dirs []string) []error {
	var errs []error
	for _, dir := range dirs {
		entries, err := os.ReadDir(dir)
		if err != nil {
			if !os.IsNotExist(err) {
				errs = append(errs, fmt.Errorf("read dir %s: %w", dir, err))
			}
			continue
		}
		for _, e := range entries {
			if e.IsDir() {
				continue
			}
			name := e.Name()
			if !strings.HasSuffix(name, ".yaml") && !strings.HasSuffix(name, ".yml") && !strings.HasSuffix(name, ".json") {
				continue
			}
			path := filepath.Join(dir, name)
			data, err := os.ReadFile(path)
			if err != nil {
				errs = append(errs, fmt.Errorf("read %s: %w", path, err))
				continue
			}
			f, err := parseOntologyFile(data)
			if err != nil {
				errs = append(errs, fmt.Errorf("parse %s: %w", path, err))
				continue
			}
			r.mu.Lock()
			r.domains = append(r.domains, f)
			r.cache = make(map[string]*ComposedOntology) // invalidate
			r.mu.Unlock()
		}
	}
	return errs
}

// LoadFile loads a single ontology file.
func (r *Registry) LoadFile(path string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read %s: %w", path, err)
	}
	f, err := parseOntologyFile(data)
	if err != nil {
		return fmt.Errorf("parse %s: %w", path, err)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.domains = append(r.domains, f)
	r.cache = make(map[string]*ComposedOntology)
	return nil
}

// LoadFromEnv loads ontology dirs from PINARD_ONTOLOGY_DIRS and, optionally,
// from a per-vignoble directory (vignoblePath/pinard/ontology/).
func (r *Registry) LoadFromEnv(vignoblePath string) []error {
	var dirs []string
	if s := os.Getenv("PINARD_ONTOLOGY_DIRS"); s != "" {
		for _, d := range strings.Split(s, ":") {
			d = strings.TrimSpace(d)
			if d != "" {
				dirs = append(dirs, d)
			}
		}
	}
	if vignoblePath != "" {
		dirs = append(dirs, filepath.Join(vignoblePath, "pinard", "ontology"))
	}
	return r.LoadDirs(dirs)
}

// Compose returns the composed ontology for group_id.
// If no domain covers this group_id, returns the pure core ontology.
func (r *Registry) Compose(groupID string) *ComposedOntology {
	r.mu.RLock()
	if c, ok := r.cache[groupID]; ok {
		r.mu.RUnlock()
		return c
	}
	r.mu.RUnlock()

	r.mu.Lock()
	defer r.mu.Unlock()
	// Double-check after acquiring write lock.
	if c, ok := r.cache[groupID]; ok {
		return c
	}

	c := r.compose(groupID)
	r.cache[groupID] = c
	return c
}

func (r *Registry) compose(groupID string) *ComposedOntology {
	// Find the domain file(s) that cover this group_id.
	// If multiple match, last one wins (same order as registration).
	var domainFile *OntologyFile
	for _, d := range r.domains {
		for _, gid := range d.GroupIDs {
			if gid == groupID {
				domainFile = d
				break
			}
		}
	}

	suppressed := make(map[string]bool)
	if domainFile != nil {
		for _, s := range domainFile.Suppressed {
			suppressed[s] = true
		}
	}
	for _, s := range r.core.Suppressed {
		suppressed[s] = true
	}

	// --- Entities (core + domain) ---
	entities := make(map[string]EntityDef)
	for role, def := range r.core.Entities {
		if !suppressed[role] {
			entities[role] = def
		}
	}
	if domainFile != nil {
		for role, def := range domainFile.Entities {
			if suppressed[role] {
				continue
			}
			// Inherit properties from parent (is_a chain).
			entities[role] = mergeInheritedProps(def, role, r.core.Entities, domainFile.Entities)
		}
	}

	// --- Edges (core + domain) ---
	edges := make(map[string]EdgeDef)
	etm := make(map[string][][2]string)

	for name, raw := range r.core.Edges {
		if suppressed[name] {
			continue
		}
		pairs := rawToPairs(raw.Pairs)
		edges[name] = EdgeDef{Description: raw.Description, Pairs: pairs}
		etm[name] = pairs
	}
	if domainFile != nil {
		for name, raw := range domainFile.Edges {
			if suppressed[name] {
				continue
			}
			pairs := rawToPairs(raw.Pairs)
			if existing, ok := etm[name]; ok {
				// Extend existing edge with domain-specific pairs.
				etm[name] = append(existing, pairs...)
				edges[name] = EdgeDef{
					Description: edges[name].Description,
					Pairs:       etm[name],
				}
			} else {
				edges[name] = EdgeDef{Description: raw.Description, Pairs: pairs}
				etm[name] = pairs
			}
		}
	}

	version := OntologyVersion{CoreVersion: CoreVersion}
	if domainFile != nil {
		version.DomainName = domainFile.Domain
		version.DomainVersion = domainFile.Version
	}

	return &ComposedOntology{
		Version:     version,
		Entities:    entities,
		Edges:       edges,
		EdgeTypeMap: etm,
	}
}

// ── JSON Schema generation ────────────────────────────────────────────────────

// JSONSchemaForRole returns the per-role JSON Schema document (as a Go map
// ready for JSON serialization) for record validation before SurrealDB upsert.
func JSONSchemaForRole(c *ComposedOntology, role string) map[string]any {
	def, ok := c.Entities[role]
	if !ok {
		return nil
	}

	props := map[string]any{
		"name":        map[string]any{"type": "string"},
		"description": map[string]any{"type": "string"},
		"version":     map[string]any{"type": "string"},
		"role":        map[string]any{"type": "string", "const": role},
	}

	var required []string
	for _, r := range def.Required {
		required = append(required, r)
	}

	for propName, propDef := range def.Properties {
		props[propName] = propertyToSchema(propDef)
	}

	schema := map[string]any{
		"$schema":    "http://json-schema.org/draft-07/schema#",
		"type":       "object",
		"properties": props,
	}
	if len(required) > 0 {
		schema["required"] = required
	}
	return schema
}

func propertyToSchema(p PropertySchema) map[string]any {
	s := map[string]any{"type": p.Type}
	if p.Description != "" {
		s["description"] = p.Description
	}
	if p.Format != "" {
		s["format"] = p.Format
	}
	if p.Minimum != nil {
		s["minimum"] = *p.Minimum
	}
	if p.Maximum != nil {
		s["maximum"] = *p.Maximum
	}
	if len(p.Enum) > 0 {
		s["enum"] = p.Enum
	}
	if p.Items != nil {
		s["items"] = propertyToSchema(*p.Items)
	}
	return s
}

// ── SurrealDB DDL generation ──────────────────────────────────────────────────

// GenerateSurrealDDL generates SurrealDB DEFINE FIELD DDL for a composed
// ontology. Replaces services/memory/surrealdb/schema_gen.py.
func GenerateSurrealDDL(c *ComposedOntology) string {
	var sb strings.Builder
	sb.WriteString("-- Generated by pinard ontology DDL generator\n")
	sb.WriteString("-- DO NOT EDIT — regenerate from the composed ontology\n\n")

	// Base entity table fields (shared by all roles).
	sb.WriteString("-- ── Base entity fields ──────────────────────────────────────────────\n")
	sb.WriteString("DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL;\n")
	sb.WriteString("DEFINE FIELD IF NOT EXISTS role        ON entity TYPE string;\n")
	sb.WriteString("DEFINE FIELD IF NOT EXISTS name        ON entity TYPE string;\n")
	sb.WriteString("DEFINE FIELD IF NOT EXISTS description ON entity TYPE string DEFAULT \"\";\n")
	sb.WriteString("DEFINE FIELD IF NOT EXISTS version     ON entity TYPE string DEFAULT \"1.0.0\";\n")
	sb.WriteString("DEFINE FIELD IF NOT EXISTS provenance  ON entity TYPE string DEFAULT \"\";\n")
	sb.WriteString("DEFINE FIELD IF NOT EXISTS manual_edit ON entity TYPE bool DEFAULT false;\n")
	sb.WriteString("DEFINE FIELD IF NOT EXISTS created_at  ON entity TYPE datetime DEFAULT time::now();\n")
	sb.WriteString("DEFINE FIELD IF NOT EXISTS updated_at  ON entity TYPE datetime DEFAULT time::now();\n")
	sb.WriteString("DEFINE FIELD IF NOT EXISTS data        ON entity TYPE object FLEXIBLE DEFAULT {};\n")
	sb.WriteString("DEFINE FIELD IF NOT EXISTS embedding   ON entity TYPE option<array<float>>;\n")
	sb.WriteString("\n")

	// Per-role fields — stored in the flexible data field at the DB level,
	// but documented here for DDL completeness and validation purposes.
	for role, def := range c.Entities {
		if len(def.Properties) == 0 {
			continue
		}
		sb.WriteString(fmt.Sprintf("-- Role: %s\n", role))
		for propName, propDef := range def.Properties {
			surrealType := jsonTypeToSurreal(propDef)
			assert := buildAssert(propName, propDef)
			line := fmt.Sprintf("DEFINE FIELD IF NOT EXISTS data.%s ON entity TYPE %s", propName, surrealType)
			if assert != "" {
				line += " " + assert
			}
			line += ";\n"
			sb.WriteString(line)
		}
		sb.WriteString("\n")
	}

	// Edge relation tables.
	sb.WriteString("-- ── Edge relation tables ───────────────────────────────────────────────\n")
	for edgeName, pairs := range c.EdgeTypeMap {
		if len(pairs) == 0 {
			continue
		}
		surrealEdge := camelToSnake(edgeName)
		sb.WriteString(fmt.Sprintf("DEFINE TABLE IF NOT EXISTS %s SCHEMAFULL TYPE RELATION IN entity OUT entity;\n", surrealEdge))
		sb.WriteString(fmt.Sprintf("DEFINE FIELD IF NOT EXISTS confidence  ON %s TYPE float DEFAULT 1.0;\n", surrealEdge))
		sb.WriteString(fmt.Sprintf("DEFINE FIELD IF NOT EXISTS description ON %s TYPE string DEFAULT \"\";\n", surrealEdge))
		sb.WriteString(fmt.Sprintf("DEFINE FIELD IF NOT EXISTS data        ON %s TYPE object FLEXIBLE DEFAULT {};\n", surrealEdge))
		sb.WriteString(fmt.Sprintf("DEFINE FIELD IF NOT EXISTS created_at  ON %s TYPE datetime DEFAULT time::now();\n", surrealEdge))
		sb.WriteString("\n")
	}

	return sb.String()
}

// ── Meta-schema validation ────────────────────────────────────────────────────

// metaSchemaCompiler is a lazily-initialised JSON Schema compiler loaded from
// the embedded meta-schema.json.  Initialised once by compileMetaSchema.
var (
	metaSchemaOnce     sync.Once
	compiledMetaSchema *jsonschema.Schema
	metaSchemaErr      error
)

func compileMetaSchema() (*jsonschema.Schema, error) {
	metaSchemaOnce.Do(func() {
		raw, err := embeddedFS.ReadFile("meta-schema.json")
		if err != nil {
			metaSchemaErr = fmt.Errorf("read embedded meta-schema.json: %w", err)
			return
		}
		doc, err := jsonschema.UnmarshalJSON(bytes.NewReader(raw))
		if err != nil {
			metaSchemaErr = fmt.Errorf("unmarshal meta-schema.json: %w", err)
			return
		}
		c := jsonschema.NewCompiler()
		if err := c.AddResource("meta-schema.json", doc); err != nil {
			metaSchemaErr = fmt.Errorf("add meta-schema resource: %w", err)
			return
		}
		compiledMetaSchema, metaSchemaErr = c.Compile("meta-schema.json")
	})
	return compiledMetaSchema, metaSchemaErr
}

// ValidateFile validates an ontology YAML/JSON file against the embedded
// meta-schema using github.com/santhosh-tekuri/jsonschema/v6.
// Returns a list of validation error strings, or nil on success.
func ValidateFile(data []byte) []string {
	// Parse YAML → JSON-compatible Go value → round-trip through JSON so the
	// jsonschema validator receives proper JSON-typed values (not YAML types).
	var raw any
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return []string{fmt.Sprintf("YAML parse error: %v", err)}
	}
	jsonBytes, err := json.Marshal(convertToJSONCompatible(raw))
	if err != nil {
		return []string{fmt.Sprintf("JSON marshal error: %v", err)}
	}
	doc, err := jsonschema.UnmarshalJSON(bytes.NewReader(jsonBytes))
	if err != nil {
		return []string{fmt.Sprintf("JSON unmarshal error: %v", err)}
	}

	sch, err := compileMetaSchema()
	if err != nil {
		return []string{fmt.Sprintf("meta-schema compile error: %v", err)}
	}
	if err := sch.Validate(doc); err != nil {
		var ve *jsonschema.ValidationError
		if isValidationError(err, &ve) {
			var msgs []string
			for _, c := range ve.Causes {
				msgs = append(msgs, c.Error())
			}
			if len(msgs) == 0 {
				msgs = []string{ve.Error()}
			}
			return msgs
		}
		return []string{err.Error()}
	}
	return nil
}

func isValidationError(err error, out **jsonschema.ValidationError) bool {
	ve, ok := err.(*jsonschema.ValidationError)
	if ok {
		*out = ve
	}
	return ok
}

// ValidateRecord validates a record's data map against the JSON Schema for its
// role in the composed ontology. Returns the first validation error string, or
// empty string on success (or when the role has no schema constraints).
func ValidateRecord(c *ComposedOntology, role string, data map[string]any) string {
	schema := JSONSchemaForRole(c, role)
	if schema == nil {
		return ""
	}
	schemaBytes, err := json.Marshal(schema)
	if err != nil {
		return fmt.Sprintf("schema marshal: %v", err)
	}
	doc, err := jsonschema.UnmarshalJSON(bytes.NewReader(schemaBytes))
	if err != nil {
		return fmt.Sprintf("unmarshal role schema: %v", err)
	}
	compiler := jsonschema.NewCompiler()
	if err := compiler.AddResource("role-schema.json", doc); err != nil {
		return fmt.Sprintf("add role-schema resource: %v", err)
	}
	sch, err := compiler.Compile("role-schema.json")
	if err != nil {
		return fmt.Sprintf("compile role schema: %v", err)
	}
	// Validate the data map: round-trip through JSON for type fidelity.
	dataBytes, err := json.Marshal(data)
	if err != nil {
		return fmt.Sprintf("marshal record data: %v", err)
	}
	recordDoc, err := jsonschema.UnmarshalJSON(bytes.NewReader(dataBytes))
	if err != nil {
		return fmt.Sprintf("unmarshal record data: %v", err)
	}
	if err := sch.Validate(recordDoc); err != nil {
		var ve *jsonschema.ValidationError
		if isValidationError(err, &ve) {
			return ve.Error()
		}
		return err.Error()
	}
	return ""
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func parseOntologyFile(data []byte) (*OntologyFile, error) {
	var f OntologyFile
	if err := yaml.Unmarshal(data, &f); err != nil {
		return nil, err
	}
	return &f, nil
}

func rawToPairs(raw [][]string) [][2]string {
	out := make([][2]string, 0, len(raw))
	for _, p := range raw {
		if len(p) == 2 {
			out = append(out, [2]string{p[0], p[1]})
		}
	}
	return out
}

// mergeInheritedProps resolves the full is_a chain recursively (grandparent →
// parent → child). Child properties always override ancestor properties on key
// collision, so the most-specific definition wins.
// A visited set prevents infinite loops from misconfigured is_a cycles.
func mergeInheritedProps(def EntityDef, role string, coreEntities, domainEntities map[string]EntityDef) EntityDef {
	visited := map[string]bool{role: true}
	return resolveIsA(def, coreEntities, domainEntities, visited)
}

func resolveIsA(def EntityDef, coreEntities, domainEntities map[string]EntityDef, visited map[string]bool) EntityDef {
	if def.IsA == "" {
		return def
	}
	if visited[def.IsA] {
		// Cycle guard — return def as-is.
		return def
	}
	visited[def.IsA] = true

	parent, ok := coreEntities[def.IsA]
	if !ok {
		parent, ok = domainEntities[def.IsA]
		if !ok {
			// Unknown ancestor — return def without merging.
			return def
		}
	}
	// Recursively resolve the parent's own chain first.
	parent = resolveIsA(parent, coreEntities, domainEntities, visited)

	// Merge: grandparent props ← parent props ← child props (child wins).
	merged := make(map[string]PropertySchema, len(parent.Properties)+len(def.Properties))
	for k, v := range parent.Properties {
		merged[k] = v
	}
	for k, v := range def.Properties {
		merged[k] = v
	}
	result := def
	result.Properties = merged
	return result
}

func jsonTypeToSurreal(p PropertySchema) string {
	if p.Type == "string" && p.Format == "date-time" {
		return "datetime"
	}
	switch p.Type {
	case "string":
		return "string"
	case "integer":
		return "int"
	case "number":
		return "float"
	case "boolean":
		return "bool"
	case "array":
		return "array"
	case "object":
		return "object FLEXIBLE"
	}
	return "string"
}

func buildAssert(field string, p PropertySchema) string {
	var parts []string
	if p.Minimum != nil {
		parts = append(parts, fmt.Sprintf("ASSERT $value >= %v", *p.Minimum))
	}
	if p.Maximum != nil {
		parts = append(parts, fmt.Sprintf("ASSERT $value <= %v", *p.Maximum))
	}
	if len(p.Enum) > 0 {
		enumJSON, _ := json.Marshal(p.Enum)
		parts = append(parts, fmt.Sprintf("ASSERT $value INSIDE %s", string(enumJSON)))
	}
	if len(parts) == 0 {
		return ""
	}
	return strings.Join(parts, " ")
}

func camelToSnake(s string) string {
	var out strings.Builder
	for i, r := range s {
		if r >= 'A' && r <= 'Z' && i > 0 {
			out.WriteRune('_')
		}
		out.WriteRune(r | 0x20) // toLower
	}
	return out.String()
}

func convertToJSONCompatible(v any) any {
	switch t := v.(type) {
	case map[string]any:
		out := make(map[string]any)
		for k, val := range t {
			out[k] = convertToJSONCompatible(val)
		}
		return out
	case map[any]any:
		out := make(map[string]any)
		for k, val := range t {
			out[fmt.Sprintf("%v", k)] = convertToJSONCompatible(val)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i, val := range t {
			out[i] = convertToJSONCompatible(val)
		}
		return out
	}
	return v
}
