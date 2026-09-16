package pressoir

import (
	"fmt"
	"os"

	"github.com/Genentech/pinard/internal/config"
)

// NewPressoir instantiates the Pressoir adapter selected by cfg.Provider().
// For "gitlab" it builds a GitLabAdapter from creds.GitLab (token + host).
// For "github" it builds a GitHubAdapter; host defaults to api.github.com,
// token comes from cfg.TokenEnv (if set) or creds.GitHubToken().
// cfg.Host and cfg.TokenEnv, when set, override the credentials defaults.
func NewPressoir(cfg config.PressoirConfig, creds *config.Credentials) (Pressoir, error) {
	switch cfg.Provider() {
	case "gitlab":
		host := cfg.Host
		if host == "" {
			host = creds.GitLab.Host
		}
		token := ""
		if cfg.TokenEnv != "" {
			token = tokenFromEnv(cfg.TokenEnv)
		}
		if token == "" {
			token = creds.Token()
		}
		return NewGitLabAdapter(host, token), nil

	case "github":
		host := cfg.Host
		if host == "" {
			host = creds.GitHubHost()
		}
		token := ""
		if cfg.TokenEnv != "" {
			token = tokenFromEnv(cfg.TokenEnv)
		}
		if token == "" {
			token = creds.GitHubToken()
		}
		return NewGitHubAdapter(host, token), nil

	default:
		return nil, fmt.Errorf("pressoir: unknown provider %q", cfg.Provider())
	}
}

// tokenFromEnv reads the value of the named environment variable.
// Returns "" when the variable is unset.
func tokenFromEnv(envVar string) string {
	return os.Getenv(envVar)
}

// RepoRefFromPath builds a RepoRef from a "owner/name" or "group/sub/name" path
// as used by GitLab. The last segment is the repo Name; everything before the last
// slash is the Owner (which may be a subgroup path on GitLab).
// Host is left empty — callers that need a specific host set it themselves.
func RepoRefFromPath(repoPath string) RepoRef {
	lastSlash := -1
	for i := len(repoPath) - 1; i >= 0; i-- {
		if repoPath[i] == '/' {
			lastSlash = i
			break
		}
	}
	if lastSlash < 0 {
		return RepoRef{Name: repoPath}
	}
	return RepoRef{
		Owner: repoPath[:lastSlash],
		Name:  repoPath[lastSlash+1:],
	}
}
