package config

import (
	"os"

	"gopkg.in/yaml.v3"
)

type Schedule struct {
	Name       string `yaml:"name"`
	Project    string `yaml:"project"`
	Cron       string `yaml:"cron"`
	Prompt     string `yaml:"prompt,omitempty"`
	Command    string `yaml:"command,omitempty"`
	Enabled    bool   `yaml:"enabled"`
	Once       bool   `yaml:"once,omitempty"`
	PollRepo   string `yaml:"poll_repo,omitempty"`
	PollNewTag bool   `yaml:"poll_new_tag,omitempty"`
}

type schedulesFile struct {
	Schedules []Schedule `yaml:"schedules"`
}

func LoadSchedules(path string) ([]Schedule, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var sf schedulesFile
	if err := yaml.Unmarshal(data, &sf); err != nil {
		return nil, err
	}
	return sf.Schedules, nil
}
