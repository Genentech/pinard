package config

import (
	"os"

	"gopkg.in/yaml.v3"
)

// ParcelleConfig is the parsed representation of a parcelle.yaml file.
type ParcelleConfig struct {
	Name         string `yaml:"name"`
	Project      string `yaml:"project"`
	Status       string `yaml:"status"`
	TargetBranch string `yaml:"target_branch"`
	Issues       []int  `yaml:"issues"`
}

// LoadParcelleConfig reads and parses a parcelle.yaml file.
// Returns a zero-value ParcelleConfig and a non-nil error if the file cannot
// be read or parsed.
func LoadParcelleConfig(path string) (ParcelleConfig, error) {
	var cfg ParcelleConfig
	data, err := os.ReadFile(path)
	if err != nil {
		return cfg, err
	}
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return cfg, err
	}
	return cfg, nil
}
