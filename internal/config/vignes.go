package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

type TestsConfig struct {
	Strategy string `yaml:"strategy"` // local | k3d | none
	Command  string `yaml:"command"`  // optional override
}

type Vigne struct {
	Path             string      `yaml:"path"`
	Repo             string      `yaml:"repo"`
	DefaultBranch    string      `yaml:"default_branch,omitempty"`
	Model            ModelConfig `yaml:"model,omitempty"`
	Process          string      `yaml:"process,omitempty"`
	AutoMerge        *bool       `yaml:"auto_merge,omitempty"`
	MonitorPostMerge *bool       `yaml:"monitor_post_merge,omitempty"`
	Tests            TestsConfig `yaml:"tests,omitempty"`
	// Runtime controls how workers for this vigne are launched:
	//   "" / "local"      — bare `pinard` on the daemon host (default)
	//   "singularity"     — wrap the worker in `singularity run --containall <binds> <sif>`
	Runtime  string   `yaml:"runtime,omitempty"`
	Sif      string   `yaml:"sif,omitempty"`        // path to the .sif image (runtime=singularity)
	Binds    []string `yaml:"binds,omitempty"`      // singularity --bind entries (host[:container[:ro]])
	NoWorktree bool   `yaml:"no_worktree,omitempty"` // run in the project path, skip git worktree (data jobs)
}

func (v *Vigne) TargetBranch() string {
	if v.DefaultBranch != "" {
		return v.DefaultBranch
	}
	return "main"
}

func (v *Vigne) ExpandedPath() string {
	p := v.Path
	if strings.HasPrefix(p, "~/") {
		home, _ := os.UserHomeDir()
		p = filepath.Join(home, p[2:])
	}
	return p
}

func (v *Vigne) ShouldAutoMerge(global bool) bool {
	if v.AutoMerge != nil {
		return *v.AutoMerge
	}
	return global
}

func (v *Vigne) WorkerModel(vignobleDefault string) string {
	if v.Model.Tier != "" {
		return v.Model.Tier
	}
	if v.Model.ID != "" {
		return v.Model.ID
	}
	if vignobleDefault != "" {
		return vignobleDefault
	}
	return "sonnet"
}

func (v *Vigne) ShouldMonitorPostMerge() bool {
	if v.MonitorPostMerge != nil {
		return *v.MonitorPostMerge
	}
	return true // default: enabled
}

type ModelConfig struct {
	ID   string `yaml:"id"`
	Tier string `yaml:"tier"`
}

type ModelsConfig struct {
	Conductor ModelConfig `yaml:"conductor,omitempty"`
	Worker    ModelConfig `yaml:"worker,omitempty"`
}

type VignobleConfig struct {
	GitLabHost  string           `yaml:"gitlab_host"`
	GitLabGroup string           `yaml:"gitlab_group"`
	AutoMerge   bool             `yaml:"auto_merge"`
	Models      ModelsConfig     `yaml:"models,omitempty"`
	Vignes      map[string]Vigne `yaml:"vignes"`
}

func LoadVignoble(configPath string) (*VignobleConfig, error) {
	data, err := os.ReadFile(configPath)
	if err != nil {
		return nil, err
	}
	var cfg VignobleConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	return &cfg, nil
}

type Vignoble struct {
	Path       string
	Name       string
	ConfigPath string
	StateDir   string
	LogDir     string
	Config     *VignobleConfig
}

func ResolveVignoble() (*Vignoble, error) {
	configPath := os.Getenv("AOC_CONFIG")
	if configPath == "" {
		cwd, _ := os.Getwd()
		candidate := filepath.Join(cwd, "vignes.yaml")
		if _, err := os.Stat(candidate); err == nil {
			configPath = candidate
		}
	}
	if configPath == "" {
		return nil, fmt.Errorf("no vignoble found — run from a vignoble directory or set AOC_CONFIG")
	}

	dir := filepath.Dir(configPath)
	name := filepath.Base(dir)
	name = strings.TrimPrefix(name, "vignoble-")

	cfg, err := LoadVignoble(configPath)
	if err != nil {
		return nil, err
	}

	return &Vignoble{
		Path:       dir,
		Name:       name,
		ConfigPath: configPath,
		StateDir:   filepath.Join(dir, ".state"),
		LogDir:     filepath.Join(dir, "logs"),
		Config:     cfg,
	}, nil
}
