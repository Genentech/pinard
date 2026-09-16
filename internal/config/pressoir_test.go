package config

import (
	"testing"
)

// makeVignoble builds a minimal Vignoble for testing PressoirConfig resolution.
func makeVignoble(vignobleConfig PressoirConfig, vignes map[string]Vigne) *Vignoble {
	return &Vignoble{
		Config: &VignobleConfig{
			GitLabHost:  "gitlab.example.com",
			GitLabGroup: "mygroup",
			Pressoir:    vignobleConfig,
			Vignes:      vignes,
		},
	}
}

// TestResolvePressoirConfig_DefaultIsGitLab verifies that a vignoble with no pressoir
// config at any level returns provider="gitlab" with inherited host/group.
func TestResolvePressoirConfig_DefaultIsGitLab(t *testing.T) {
	v := makeVignoble(PressoirConfig{}, map[string]Vigne{
		"my-repo": {Repo: "mygroup/my-repo"},
	})

	got := v.ResolvePressoirConfig("my-repo")
	if got.Provider() != "gitlab" {
		t.Errorf("Provider: got %q, want %q", got.Provider(), "gitlab")
	}
	if got.Host != "gitlab.example.com" {
		t.Errorf("Host: got %q, want %q", got.Host, "gitlab.example.com")
	}
	if got.Org != "mygroup" {
		t.Errorf("Org: got %q, want %q", got.Org, "mygroup")
	}
}

// TestResolvePressoirConfig_UnknownRepoDefaultsToGitLab verifies that a repo not
// in the vignes map also falls through to the gitlab default.
func TestResolvePressoirConfig_UnknownRepoDefaultsToGitLab(t *testing.T) {
	v := makeVignoble(PressoirConfig{}, map[string]Vigne{})

	got := v.ResolvePressoirConfig("nonexistent-repo")
	if got.Provider() != "gitlab" {
		t.Errorf("Provider: got %q, want %q", got.Provider(), "gitlab")
	}
}

// TestResolvePressoirConfig_VignobleOverride verifies that a vignoble-level pressoir
// setting applies to all repos that don't have a per-repo override.
func TestResolvePressoirConfig_VignobleOverride(t *testing.T) {
	v := makeVignoble(PressoirConfig{
		ProviderName: "github",
		Host:         "github.com",
		Org:          "myorg",
		TokenEnv:     "GITHUB_TOKEN",
	}, map[string]Vigne{
		"my-repo": {Repo: "myorg/my-repo"},
	})

	got := v.ResolvePressoirConfig("my-repo")
	if got.Provider() != "github" {
		t.Errorf("Provider: got %q, want %q", got.Provider(), "github")
	}
	if got.Host != "github.com" {
		t.Errorf("Host: got %q, want %q", got.Host, "github.com")
	}
	if got.Org != "myorg" {
		t.Errorf("Org: got %q, want %q", got.Org, "myorg")
	}
	if got.TokenEnv != "GITHUB_TOKEN" {
		t.Errorf("TokenEnv: got %q, want %q", got.TokenEnv, "GITHUB_TOKEN")
	}
}

// TestResolvePressoirConfig_RepoOverrideBeatsVignoble verifies that a per-repo
// pressoir setting takes precedence over the vignoble-level setting.
func TestResolvePressoirConfig_RepoOverrideBeatsVignoble(t *testing.T) {
	v := makeVignoble(PressoirConfig{
		ProviderName: "github",
		Host:         "github.com",
		Org:          "myorg",
	}, map[string]Vigne{
		"legacy-repo": {
			Repo: "mygroup/legacy-repo",
			Pressoir: PressoirConfig{
				ProviderName: "gitlab",
				Host:         "internal-gitlab.example.com",
				Org:          "mygroup",
				TokenEnv:     "GL_TOKEN",
			},
		},
		"github-repo": {Repo: "myorg/github-repo"},
	})

	// Per-repo override wins for legacy-repo.
	got := v.ResolvePressoirConfig("legacy-repo")
	if got.Provider() != "gitlab" {
		t.Errorf("legacy-repo Provider: got %q, want %q", got.Provider(), "gitlab")
	}
	if got.Host != "internal-gitlab.example.com" {
		t.Errorf("legacy-repo Host: got %q, want %q", got.Host, "internal-gitlab.example.com")
	}
	if got.TokenEnv != "GL_TOKEN" {
		t.Errorf("legacy-repo TokenEnv: got %q, want %q", got.TokenEnv, "GL_TOKEN")
	}

	// Vignoble-level applies to github-repo (no per-repo override).
	got2 := v.ResolvePressoirConfig("github-repo")
	if got2.Provider() != "github" {
		t.Errorf("github-repo Provider: got %q, want %q", got2.Provider(), "github")
	}
}

// TestResolvePressoirConfig_RepoOverrideBeatsDefault verifies that a per-repo
// pressoir setting takes precedence over the built-in gitlab default (no vignoble pressoir set).
func TestResolvePressoirConfig_RepoOverrideBeatsDefault(t *testing.T) {
	v := makeVignoble(PressoirConfig{}, map[string]Vigne{
		"gh-repo": {
			Repo: "myorg/gh-repo",
			Pressoir: PressoirConfig{
				ProviderName: "github",
				Org:          "myorg",
				TokenEnv:     "GITHUB_TOKEN",
			},
		},
	})

	got := v.ResolvePressoirConfig("gh-repo")
	if got.Provider() != "github" {
		t.Errorf("Provider: got %q, want %q", got.Provider(), "github")
	}
	if got.Org != "myorg" {
		t.Errorf("Org: got %q, want %q", got.Org, "myorg")
	}
}

// TestResolvePressoirConfig_ByRepoPath verifies that resolution works when the caller
// passes the full repo path (e.g. "sirloon/exohub-test") — the primary call-site pattern.
// This is the main bug scenario: vigne map key is "exohub-test" but callers pass the path.
func TestResolvePressoirConfig_ByRepoPath(t *testing.T) {
	v := makeVignoble(PressoirConfig{}, map[string]Vigne{
		"exohub-test": {
			Repo: "sirloon/exohub-test",
			Pressoir: PressoirConfig{
				ProviderName: "github",
				Org:          "sirloon",
				TokenEnv:     "GITHUB_TOKEN",
			},
		},
	})

	got := v.ResolvePressoirConfig("sirloon/exohub-test")
	if got.Provider() != "github" {
		t.Errorf("Provider: got %q, want %q", got.Provider(), "github")
	}
	if got.Org != "sirloon" {
		t.Errorf("Org: got %q, want %q", got.Org, "sirloon")
	}
	if got.TokenEnv != "GITHUB_TOKEN" {
		t.Errorf("TokenEnv: got %q, want %q", got.TokenEnv, "GITHUB_TOKEN")
	}
}

// TestResolvePressoirConfig_ByVigneName verifies that the secondary map-key lookup
// still works when the caller passes the short vigne name instead of the repo path.
func TestResolvePressoirConfig_ByVigneName(t *testing.T) {
	v := makeVignoble(PressoirConfig{}, map[string]Vigne{
		"exohub-test": {
			Repo: "sirloon/exohub-test",
			Pressoir: PressoirConfig{
				ProviderName: "github",
				Org:          "sirloon",
				TokenEnv:     "GITHUB_TOKEN",
			},
		},
	})

	got := v.ResolvePressoirConfig("exohub-test")
	if got.Provider() != "github" {
		t.Errorf("Provider: got %q, want %q", got.Provider(), "github")
	}
}

// TestResolvePressoirConfig_UnknownRepoPathFallsToGitLab verifies that a repo path
// that does not match any vigne still returns the gitlab default.
func TestResolvePressoirConfig_UnknownRepoPathFallsToGitLab(t *testing.T) {
	v := makeVignoble(PressoirConfig{}, map[string]Vigne{
		"exohub-test": {Repo: "sirloon/exohub-test"},
	})

	got := v.ResolvePressoirConfig("unknown/repo")
	if got.Provider() != "gitlab" {
		t.Errorf("Provider: got %q, want %q", got.Provider(), "gitlab")
	}
	if got.Host != "gitlab.example.com" {
		t.Errorf("Host: got %q, want %q", got.Host, "gitlab.example.com")
	}
}

// TestResolvePressoirConfig_VignobleOverrideByRepoPath verifies that a vigne with no
// per-repo pressoir override inherits the vignoble-level setting when looked up by
// either the repo path or the vigne name.
func TestResolvePressoirConfig_VignobleOverrideByRepoPath(t *testing.T) {
	v := makeVignoble(PressoirConfig{
		ProviderName: "github",
		Host:         "github.com",
		Org:          "myorg",
		TokenEnv:     "GITHUB_TOKEN",
	}, map[string]Vigne{
		"myrepo": {Repo: "myorg/myrepo"}, // no per-repo pressoir
	})

	for _, arg := range []string{"myorg/myrepo", "myrepo"} {
		got := v.ResolvePressoirConfig(arg)
		if got.Provider() != "github" {
			t.Errorf("arg=%q Provider: got %q, want %q", arg, got.Provider(), "github")
		}
		if got.Host != "github.com" {
			t.Errorf("arg=%q Host: got %q, want %q", arg, got.Host, "github.com")
		}
	}
}

// TestPressoirConfig_Provider_Empty verifies that Provider() defaults to "gitlab" for zero value.
func TestPressoirConfig_Provider_Empty(t *testing.T) {
	var f PressoirConfig
	if f.Provider() != "gitlab" {
		t.Errorf("Provider(): got %q, want %q", f.Provider(), "gitlab")
	}
}

// TestPressoirConfig_Provider_Set verifies that Provider() returns the set value.
func TestPressoirConfig_Provider_Set(t *testing.T) {
	f := PressoirConfig{ProviderName: "github"}
	if f.Provider() != "github" {
		t.Errorf("Provider(): got %q, want %q", f.Provider(), "github")
	}
}
