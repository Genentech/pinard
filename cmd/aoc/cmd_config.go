package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"
)

var configCmd = &cobra.Command{
	Use:   "config",
	Short: "Get or set vignes.yaml configuration",
}

var configSetCmd = &cobra.Command{
	Use:   "set <path> <value>",
	Short: "Set a config value using dot-path notation",
	Long: `Set a config value in vignes.yaml using dot-path notation.

Examples:
  aoc config set models.worker.id claude-opus-4-6
  aoc config set models.conductor.id claude-sonnet-4-6
  aoc config set vignes.charon.model.id claude-opus-4-6
  aoc config set vignes.charon.auto_merge true
  aoc config set auto_merge false`,
	Args: cobra.ExactArgs(2),
	RunE: func(cmd *cobra.Command, args []string) error {
		return configSet(args[0], args[1])
	},
}

var configGetCmd = &cobra.Command{
	Use:   "get <path>",
	Short: "Get a config value using dot-path notation",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		val, err := configGet(args[0])
		if err != nil {
			return err
		}
		fmt.Println(val)
		return nil
	},
}

func init() {
	configCmd.AddCommand(configSetCmd)
	configCmd.AddCommand(configGetCmd)
	rootCmd.AddCommand(configCmd)
}

func configPath() (string, error) {
	p := os.Getenv("AOC_CONFIG")
	if p != "" {
		return p, nil
	}
	cwd, _ := os.Getwd()
	candidate := filepath.Join(cwd, "vignes.yaml")
	if _, err := os.Stat(candidate); err == nil {
		return candidate, nil
	}
	return "", fmt.Errorf("vignes.yaml not found — run from a vignoble directory or set AOC_CONFIG")
}

func validatePath(path string) error {
	parts := strings.Split(path, ".")
	if len(parts) == 0 {
		return fmt.Errorf("empty path")
	}

	switch parts[0] {
	case "models":
		// models.conductor.id, models.worker.id
		if len(parts) != 3 {
			return fmt.Errorf("models path must be models.<role>.id")
		}
		if parts[1] != "conductor" && parts[1] != "worker" {
			return fmt.Errorf("models role must be 'conductor' or 'worker', got %q", parts[1])
		}
		if parts[2] != "id" {
			return fmt.Errorf("only 'id' field supported under models.<role>, got %q", parts[2])
		}
	case "vignes":
		// vignes.<name>.<field> or vignes.<name>.model.id
		if len(parts) < 3 {
			return fmt.Errorf("vignes path must be vignes.<name>.<field>")
		}
		field := parts[2]
		switch field {
		case "model":
			if len(parts) != 4 || parts[3] != "id" {
				return fmt.Errorf("vignes.<name>.model path must end with .id")
			}
		case "auto_merge", "monitor_post_merge", "path", "repo":
			if len(parts) != 3 {
				return fmt.Errorf("vignes.<name>.%s takes no sub-path", field)
			}
		default:
			return fmt.Errorf("unknown vigne field %q (allowed: model.id, auto_merge, monitor_post_merge, path, repo)", field)
		}
	case "auto_merge", "gitlab_host", "gitlab_group":
		if len(parts) != 1 {
			return fmt.Errorf("%s takes no sub-path", parts[0])
		}
	default:
		return fmt.Errorf("unknown top-level key %q (allowed: models, vignes, auto_merge, gitlab_host, gitlab_group)", parts[0])
	}
	return nil
}

func configSet(path, value string) error {
	if err := validatePath(path); err != nil {
		return err
	}

	cfgPath, err := configPath()
	if err != nil {
		return err
	}

	data, err := os.ReadFile(cfgPath)
	if err != nil {
		return err
	}

	var doc yaml.Node
	if err := yaml.Unmarshal(data, &doc); err != nil {
		return fmt.Errorf("invalid YAML: %w", err)
	}

	if doc.Kind != yaml.DocumentNode || len(doc.Content) == 0 {
		return fmt.Errorf("unexpected YAML structure")
	}
	root := doc.Content[0]
	if root.Kind != yaml.MappingNode {
		return fmt.Errorf("root is not a mapping")
	}

	parts := strings.Split(path, ".")
	if err := setNodeValue(root, parts, value); err != nil {
		return err
	}

	out, err := yaml.Marshal(&doc)
	if err != nil {
		return err
	}

	tmp := cfgPath + ".tmp"
	if err := os.WriteFile(tmp, out, 0644); err != nil {
		return err
	}
	if err := os.Rename(tmp, cfgPath); err != nil {
		os.Remove(tmp)
		return err
	}

	fmt.Printf("Set %s = %s\n", path, value)
	return nil
}

func configGet(path string) (string, error) {
	if err := validatePath(path); err != nil {
		return "", err
	}

	cfgPath, err := configPath()
	if err != nil {
		return "", err
	}

	data, err := os.ReadFile(cfgPath)
	if err != nil {
		return "", err
	}

	var doc yaml.Node
	if err := yaml.Unmarshal(data, &doc); err != nil {
		return "", fmt.Errorf("invalid YAML: %w", err)
	}

	if doc.Kind != yaml.DocumentNode || len(doc.Content) == 0 {
		return "", fmt.Errorf("unexpected YAML structure")
	}
	root := doc.Content[0]

	parts := strings.Split(path, ".")
	node := findNode(root, parts)
	if node == nil {
		return "", fmt.Errorf("path %q not set", path)
	}
	return node.Value, nil
}

func findNode(node *yaml.Node, path []string) *yaml.Node {
	if len(path) == 0 {
		return node
	}
	if node.Kind != yaml.MappingNode {
		return nil
	}
	key := path[0]
	for i := 0; i < len(node.Content)-1; i += 2 {
		if node.Content[i].Value == key {
			return findNode(node.Content[i+1], path[1:])
		}
	}
	return nil
}

func setNodeValue(node *yaml.Node, path []string, value string) error {
	if len(path) == 1 {
		key := path[0]
		for i := 0; i < len(node.Content)-1; i += 2 {
			if node.Content[i].Value == key {
				node.Content[i+1].Value = value
				node.Content[i+1].Tag = "!!str"
				node.Content[i+1].Kind = yaml.ScalarNode
				// Handle booleans
				if value == "true" || value == "false" {
					node.Content[i+1].Tag = "!!bool"
				}
				return nil
			}
		}
		// Key doesn't exist — add it
		keyNode := &yaml.Node{Kind: yaml.ScalarNode, Value: key, Tag: "!!str"}
		valNode := &yaml.Node{Kind: yaml.ScalarNode, Value: value, Tag: "!!str"}
		if value == "true" || value == "false" {
			valNode.Tag = "!!bool"
		}
		node.Content = append(node.Content, keyNode, valNode)
		return nil
	}

	key := path[0]
	for i := 0; i < len(node.Content)-1; i += 2 {
		if node.Content[i].Value == key {
			child := node.Content[i+1]
			if child.Kind != yaml.MappingNode {
				// Convert to mapping
				child.Kind = yaml.MappingNode
				child.Content = nil
				child.Value = ""
				child.Tag = "!!map"
			}
			return setNodeValue(child, path[1:], value)
		}
	}

	// Key doesn't exist — create intermediate mapping
	keyNode := &yaml.Node{Kind: yaml.ScalarNode, Value: key, Tag: "!!str"}
	mapNode := &yaml.Node{Kind: yaml.MappingNode, Tag: "!!map"}
	node.Content = append(node.Content, keyNode, mapNode)
	return setNodeValue(mapNode, path[1:], value)
}
