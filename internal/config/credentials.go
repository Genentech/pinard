package config

import (
	"os"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

type GitLabConfig struct {
	Host          string `yaml:"host"`
	User          string `yaml:"user"`
	Reviewer      string `yaml:"reviewer"`
	TokenEnv      string `yaml:"token_env"`
	OwnerTokenEnv string `yaml:"owner_token_env"` // optional: human operator's token for owner-attributed API calls
	SSHKey        string `yaml:"ssh_key"`
	GitName       string `yaml:"git_name"`
	GitEmail      string `yaml:"git_email"`
}

type NATSConfig struct {
	URL         string `yaml:"url"`
	User        string `yaml:"user"`
	PasswordEnv string `yaml:"password_env"`
	Credentials string `yaml:"credentials"`
}

// EngramConfig points local-first engram at the central `engram cloud serve`
// backend for replication. Optional — absent means engram stays purely local.
type EngramConfig struct {
	Server        string `yaml:"server"`          // e.g. https://engram.example.com
	CloudToken    string `yaml:"cloud_token"`     // literal (e.g. delivered via a secret URL)
	CloudTokenEnv string `yaml:"cloud_token_env"` // or env-var indirection
}

// WebtermConfig configures the web-terminal-access gateway/responder (Phase 1:
// signed-link, read-only, SSO-deferred). Secrets follow the literal-or-env
// pattern used elsewhere (EngramConfig): a literal value wins, else the *_env
// var is read. LinkSecret signs the browser-facing links; GrantSecret signs the
// short-lived gateway→responder authorization grants. Both must be shared:
// LinkSecret between the link builder (aoc track-mr) and the gateway; GrantSecret
// between the gateway and every responder.
type WebtermConfig struct {
	BaseURL string `yaml:"base_url"` // e.g. https://pinard.example.com

	LinkSecretLiteral string `yaml:"link_secret"`
	LinkSecretEnv     string `yaml:"link_secret_env"`

	GrantSecretLiteral string `yaml:"grant_secret"`
	GrantSecretEnv     string `yaml:"grant_secret_env"`

	LinkTTL     string `yaml:"link_ttl"`     // Go duration, default 2h
	IdleTimeout string `yaml:"idle_timeout"` // Go duration, default 10m
	MaxViewers  int    `yaml:"max_viewers"`  // per responder, default 8

	// PostLinks gates whether `aoc track-mr` posts the terminal link on the MR.
	// Default false: until SSO (Phase 2) gates the gateway, we do not advertise
	// links on issues/MRs. Flip to true once authentication is enforced.
	PostLinks bool `yaml:"post_links"`

	// Auth configures in-gateway Cognito OIDC (Phase 2). Enforced only when
	// Issuer + ClientID are set; otherwise the gateway keeps Phase-1 behavior
	// (signed link is the sole gate).
	Auth WebtermAuthConfig `yaml:"auth"`
}

// WebtermAuthConfig configures the gateway's in-gateway OIDC login flow
// (Authorization Code + PKCE; public client, no secret). Endpoints are
// discovered from <Issuer>/.well-known/openid-configuration.
type WebtermAuthConfig struct {
	Issuer      string   `yaml:"issuer"`       // e.g. https://cognito-idp.us-east-1.amazonaws.com/us-east-1_YourPoolId
	ClientID    string   `yaml:"client_id"`    // Cognito app client id (token aud)
	RedirectURL string   `yaml:"redirect_url"` // default: <base_url>/sessions/auth/callback
	Scopes      []string `yaml:"scopes"`       // default: openid email profile

	CookieSecretLiteral string `yaml:"cookie_secret"`
	CookieSecretEnv     string `yaml:"cookie_secret_env"`

	SessionTTL string `yaml:"session_ttl"` // Go duration, default 8h
}

type Credentials struct {
	GitLab  GitLabConfig  `yaml:"gitlab"`
	NATS    NATSConfig    `yaml:"nats"`
	Engram  EngramConfig  `yaml:"engram"`
	Webterm WebtermConfig `yaml:"webterm"`
	// Owner is the human tenant of this vignoble (used for web-terminal operator
	// authorization). Defaults to NATS.User when empty. See WebtermOwner.
	Owner string `yaml:"owner"`
}

func (c *Credentials) Token() string {
	if c.GitLab.TokenEnv == "" {
		return ""
	}
	return os.Getenv(c.GitLab.TokenEnv)
}

// OwnerToken returns the human operator's GitLab token (for owner-attributed API
// calls such as issue assignment in spawn_agent). Empty if not configured.
func (c *Credentials) OwnerToken() string {
	if c.GitLab.OwnerTokenEnv == "" {
		return ""
	}
	return os.Getenv(c.GitLab.OwnerTokenEnv)
}

func (c *Credentials) NATSPassword() string {
	if c.NATS.PasswordEnv == "" {
		return ""
	}
	return os.Getenv(c.NATS.PasswordEnv)
}

func (c *Credentials) NATSUrl() string {
	return c.NATS.URL
}

// EngramCloudToken returns the shared engram cloud auth token, preferring a literal
// value over the env-var indirection. Empty = cloud replication disabled.
func (c *Credentials) EngramCloudToken() string {
	if c.Engram.CloudToken != "" {
		return c.Engram.CloudToken
	}
	if c.Engram.CloudTokenEnv == "" {
		return ""
	}
	return os.Getenv(c.Engram.CloudTokenEnv)
}

func (c *Credentials) EngramServer() string {
	return c.Engram.Server
}

// ── Webterm accessors ──────────────────────────────────────────

func secretValue(literal, envName string) []byte {
	if literal != "" {
		return []byte(literal)
	}
	if envName != "" {
		if v := os.Getenv(envName); v != "" {
			return []byte(v)
		}
	}
	return nil
}

func (c *Credentials) WebtermLinkSecret() []byte {
	return secretValue(c.Webterm.LinkSecretLiteral, c.Webterm.LinkSecretEnv)
}

func (c *Credentials) WebtermGrantSecret() []byte {
	return secretValue(c.Webterm.GrantSecretLiteral, c.Webterm.GrantSecretEnv)
}

func (c *Credentials) WebtermBaseURL() string {
	return strings.TrimRight(c.Webterm.BaseURL, "/")
}

func (c *Credentials) WebtermLinkTTL() time.Duration {
	if d, err := time.ParseDuration(c.Webterm.LinkTTL); err == nil && d > 0 {
		return d
	}
	return 2 * time.Hour
}

func (c *Credentials) WebtermIdleTimeout() time.Duration {
	if d, err := time.ParseDuration(c.Webterm.IdleTimeout); err == nil && d > 0 {
		return d
	}
	return 10 * time.Minute
}

func (c *Credentials) WebtermPostLinks() bool {
	return c.Webterm.PostLinks
}

// WebtermOwner is the vignoble owner username for operator authorization: the
// explicit `owner:` override, else the NATS user (the human tenant).
func (c *Credentials) WebtermOwner() string {
	if c.Owner != "" {
		return c.Owner
	}
	return c.NATS.User
}

func (c *Credentials) WebtermCookieSecret() []byte {
	return secretValue(c.Webterm.Auth.CookieSecretLiteral, c.Webterm.Auth.CookieSecretEnv)
}

// WebtermAuthEnabled reports whether the gateway should enforce Cognito login:
// issuer + client id present. Absent → Phase-1 signed-link-only behavior.
func (c *Credentials) WebtermAuthEnabled() bool {
	return c.Webterm.Auth.Issuer != "" && c.Webterm.Auth.ClientID != ""
}

// WebtermRedirectURL returns the OIDC callback URL, defaulting to
// <base_url>/sessions/auth/callback when not set explicitly.
func (c *Credentials) WebtermRedirectURL() string {
	if c.Webterm.Auth.RedirectURL != "" {
		return c.Webterm.Auth.RedirectURL
	}
	if b := c.WebtermBaseURL(); b != "" {
		return b + "/sessions/auth/callback"
	}
	return ""
}

func (c *Credentials) WebtermScopes() []string {
	if len(c.Webterm.Auth.Scopes) > 0 {
		return c.Webterm.Auth.Scopes
	}
	return []string{"openid", "email", "profile"}
}

func (c *Credentials) WebtermSessionTTL() time.Duration {
	if d, err := time.ParseDuration(c.Webterm.Auth.SessionTTL); err == nil && d > 0 {
		return d
	}
	return 8 * time.Hour
}

func (c *Credentials) WebtermMaxViewers() int {
	if c.Webterm.MaxViewers > 0 {
		return c.Webterm.MaxViewers
	}
	return 8
}

// WebtermEnabled reports whether link generation/verification is configured:
// a base URL plus both signing secrets present.
func (c *Credentials) WebtermEnabled() bool {
	return c.WebtermBaseURL() != "" && len(c.WebtermLinkSecret()) > 0 && len(c.WebtermGrantSecret()) > 0
}

// WebtermResponderEnabled reports whether a responder can run: only the grant
// secret is required host-side (the link/base_url live on the gateway).
func (c *Credentials) WebtermResponderEnabled() bool {
	return len(c.WebtermGrantSecret()) > 0
}

func (c *Credentials) SSHKeyPath() string {
	p := c.GitLab.SSHKey
	if strings.HasPrefix(p, "~/") {
		home, _ := os.UserHomeDir()
		p = filepath.Join(home, p[2:])
	}
	return p
}

func LoadCredentials() (*Credentials, error) {
	path := os.Getenv("PINARD_CREDENTIALS")
	if path == "" {
		home, _ := os.UserHomeDir()
		path = filepath.Join(home, ".config", "pinard", "credentials.yaml")
	}

	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return &Credentials{}, nil
		}
		return nil, err
	}

	var creds Credentials
	if err := yaml.Unmarshal(data, &creds); err != nil {
		return nil, err
	}

	// Defaults
	if creds.GitLab.User == "" {
		creds.GitLab.User = "pinard"
	}

	return &creds, nil
}
