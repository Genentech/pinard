package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/engram"
	"github.com/spf13/cobra"
)

// These commands replace the inline python3/awk parsing the bash launcher used to
// do, so `python3` is no longer a runtime prerequisite. They emit plain stdout the
// launcher consumes.

// aoc resolve-model — resolve a tier (sonnet|opus|haiku) or model ID to the concrete
// model ID from ~/.claude/settings.json. With --role/--project, derive the tier from
// vignes.yaml first. With --models-list, print the conductor's proxy/<id>,... list.
var resolveModelCmd = &cobra.Command{
	Use:   "resolve-model [tier-or-id]",
	Short: "Resolve a model tier to its concrete model ID (from ~/.claude/settings.json)",
	RunE: func(cmd *cobra.Command, args []string) error {
		modelsList, _ := cmd.Flags().GetBool("models-list")
		if modelsList {
			opus, sonnet, haiku := config.SettingsModels()
			var parts []string
			for _, id := range []string{opus, sonnet, haiku} {
				if id != "" {
					parts = append(parts, "proxy/"+id)
				}
			}
			fmt.Println(strings.Join(parts, ","))
			return nil
		}

		role, _ := cmd.Flags().GetString("role")
		project, _ := cmd.Flags().GetString("project")

		// Determine the tier/id to resolve: explicit arg wins, else derive from config.
		var want string
		if len(args) > 0 && args[0] != "" {
			want = args[0]
		} else if role != "" {
			vb, err := config.ResolveVignoble()
			if err != nil {
				return err
			}
			switch role {
			case "worker":
				def := vb.Config.Models.Worker.Tier
				if def == "" {
					def = vb.Config.Models.Worker.ID
				}
				if project != "" {
					if vigne, ok := vb.Config.Vignes[project]; ok {
						want = vigne.WorkerModel(def)
					}
				}
				if want == "" {
					want = def
				}
				if want == "" {
					want = "sonnet"
				}
			case "conductor":
				want = vb.Config.Models.Conductor.Tier
				if want == "" {
					want = vb.Config.Models.Conductor.ID
				}
				if want == "" {
					want = "opus"
				}
			default:
				return fmt.Errorf("unknown role %q (want worker|conductor)", role)
			}
		}

		fmt.Println(config.ResolveModelTier(want))
		return nil
	},
}

// aoc vigne-args — emit the worker BABYSITTER_ARGS JSON for a project. Vigne-derived
// fields (repo, encodedRepo, tests) come from vignes.yaml; per-run fields come from flags.
var vigneArgsCmd = &cobra.Command{
	Use:   "vigne-args --project <name>",
	Short: "Emit worker process args (BABYSITTER_ARGS JSON) for a project",
	RunE: func(cmd *cobra.Command, args []string) error {
		project, _ := cmd.Flags().GetString("project")
		if project == "" {
			return fmt.Errorf("--project is required")
		}

		// Vigne-derived fields. With --repo (vignoble-free mode, e.g. a remote
		// worker without the vignoble dir) take them from flags; otherwise read
		// vignes.yaml.
		repo, _ := cmd.Flags().GetString("repo")
		host, _ := cmd.Flags().GetString("host")
		testStrategy, _ := cmd.Flags().GetString("test-strategy")
		testCommand, _ := cmd.Flags().GetString("test-command")
		if repo == "" {
			vb, err := config.ResolveVignoble()
			if err != nil {
				return err
			}
			vigne, ok := vb.Config.Vignes[project]
			if !ok {
				return fmt.Errorf("project %q not found in vignes.yaml", project)
			}
			repo = vigne.Repo
			host = vb.Config.GitLabHost
			testStrategy = vigne.Tests.Strategy
			testCommand = vigne.Tests.Command
		}
		if testStrategy == "" {
			testStrategy = "local"
		}
		session, _ := cmd.Flags().GetString("session")
		targetBranch, _ := cmd.Flags().GetString("target-branch")
		issue, _ := cmd.Flags().GetString("issue")
		parcelle, _ := cmd.Flags().GetString("parcelle")
		runID, _ := cmd.Flags().GetString("run-id")
		reviewer, _ := cmd.Flags().GetString("reviewer")
		assignee, _ := cmd.Flags().GetString("assignee")

		out := map[string]string{
			"project":      project,
			"host":         host,
			"repo":         repo,
			"encodedRepo":  url.QueryEscape(repo),
			"targetBranch": targetBranch,
			"session":      session,
			"assignee":     assignee,
			"reviewer":     reviewer,
			"testStrategy": testStrategy,
			"testCommand":  testCommand,
			"issueId":      issue,
			"issue":        issue,
			"parcelle":     parcelle,
			"runId":        runID,
			"prompt":       "",
		}
		b, err := json.Marshal(out)
		if err != nil {
			return err
		}
		fmt.Println(string(b))
		return nil
	},
}

// aoc env-exports — load credentials and print shell `export` lines. The launcher does
// `eval "$(aoc env-exports)"`. Resolves the token_env/password_env indirection in Go.
var envExportsCmd = &cobra.Command{
	Use:   "env-exports",
	Short: "Print shell export lines for credentials (eval in the launcher)",
	RunE: func(cmd *cobra.Command, args []string) error {
		role, _ := cmd.Flags().GetString("role")
		creds, err := config.LoadCredentials()
		if err != nil {
			return err
		}

		emit := func(k, v string) {
			if v != "" {
				fmt.Printf("export %s=%s\n", k, shquote(v))
			}
		}

		if u := creds.GitLab.User; u != "" {
			emit("PINARD_GITLAB_USER", u)
		}
		// glab's API host. Git remotes use ssh.<gitlab-host> (push only); glab must
		// target the API host. Export it so glab defaults correctly
		// instead of deriving the SSH remote hostname.
		gitlabHost := creds.GitLab.Host
		emit("GITLAB_HOST", gitlabHost)
		if tok := creds.Token(); tok != "" {
			emit("GITLAB_TOKEN", tok)
			emit("GLAB_TOKEN", tok)
		} else if creds.GitLab.TokenEnv != "" {
			fmt.Fprintf(os.Stderr, "# warning: %s is not set — GitLab API calls will fail\n", creds.GitLab.TokenEnv)
		}
		// Owner token: conductor-only — never emitted for workers to prevent an
		// LLM-driven vendangeur from holding the operator's GitLab PAT and bypassing
		// the owner-gate it was designed to protect.
		if role == "conductor" {
			if ownerTok := creds.OwnerToken(); ownerTok != "" {
				emit("PINARD_OWNER_GITLAB_TOKEN", ownerTok)
			}
		}
		if key := creds.SSHKeyPath(); key != "" {
			emit("GIT_SSH_COMMAND", fmt.Sprintf("ssh -i %s -o IdentitiesOnly=yes", key))
		}
		// Committer = pinard (pushes via pinard key). Author stays the user's gitconfig.
		emit("GIT_COMMITTER_NAME", creds.GitLab.GitName)
		emit("GIT_COMMITTER_EMAIL", creds.GitLab.GitEmail)

		emit("PINARD_NATS_USER", creds.NATS.User)
		emit("PINARD_NATS_URL", creds.NATS.URL)
		if pass := creds.NATSPassword(); pass != "" {
			emit("PINARD_NATS_PASSWORD", pass)
		} else if creds.NATS.PasswordEnv != "" {
			fmt.Fprintf(os.Stderr, "# warning: %s is not set — NATS auth will fail\n", creds.NATS.PasswordEnv)
		}

		// Engram central backend (optional). Emitted only when configured; the
		// bin/pinard consumes these to run `engram cloud config`, `enroll`, and
		// `sync --cloud` (non-fatal; absence leaves engram purely local).
		emit("ENGRAM_CLOUD_SERVER", creds.EngramServer())
		if tok := creds.EngramCloudToken(); tok != "" {
			emit("ENGRAM_CLOUD_TOKEN", tok)
		} else if creds.Engram.CloudTokenEnv != "" {
			fmt.Fprintf(os.Stderr, "# warning: %s is not set — engram cloud replication disabled\n", creds.Engram.CloudTokenEnv)
		}

		// Per-vignoble engram serve port + URL — the authoritative single source of
		// truth. The launcher uses these instead of cksum-computing or honoring a
		// stale inherited ENGRAM_PORT (which would misroute a vignoble's agents to a
		// DIFFERENT vignoble's serve). Best-effort: emitted only when a vignoble
		// resolves (cwd / AOC_CONFIG). Standalone/remote workers (no vignoble, no aoc)
		// don't get these — they compute their own port and run their own serve.
		if vb, verr := config.ResolveVignoble(); verr == nil && vb.Name != "" {
			port := engram.PortForVignoble(vb.Name)
			emit("ENGRAM_PORT", strconv.Itoa(port))
			emit("ENGRAM_URL", fmt.Sprintf("http://127.0.0.1:%d", port))
		}

		return nil
	},
}

// shquote single-quotes a value for safe shell eval, escaping embedded single quotes.
func shquote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'"
}

// aoc ensure-proxy-provider — make pi aware of the LLM proxy provider at startup.
// pi reads the provider registry from ~/.pi/agent/models.json; a sandboxed worker
// (singularity --containall) starts with an empty ~/.pi, so pi has no "proxy"
// provider and `--models proxy/<id>` resolves to nothing ("No models available").
//
// Sources tried in order:
//  1. ~/.claude/settings.json (backward-compatible; mounts on non-sandboxed hosts)
//  2. Image-baked defaults: PINARD_PROXY_BASE_URL + PINARD_PROXY_HEADERS env vars,
//     token minted from PINARD_CAPSULE_CONTRACT (capsule-funded runs) or
//     PINARD_POUR_URL (sandboxed --containall workers).
//
// Idempotent: if models.json already defines a proxy provider, it is left as-is.
var ensureProxyProviderCmd = &cobra.Command{
	Use:   "ensure-proxy-provider",
	Short: "Seed ~/.pi/agent/{models,auth}.json from settings.json or baked defaults + PINARD_POUR_URL",
	RunE: func(cmd *cobra.Command, args []string) error {
		home, err := os.UserHomeDir()
		if err != nil {
			return nil
		}
		// Honor PI_CODING_AGENT_DIR so a capsule worker's isolated agent dir
		// (injected by `aoc spawn`) is used instead of the shared ~/.pi/agent.
		// Without this, ensure-proxy-provider always writes to the shared dir,
		// finds the operator token already there, and skips capsule-redeem.
		agentDir := filepath.Join(home, ".pi", "agent")
		if d := os.Getenv("PI_CODING_AGENT_DIR"); d != "" {
			agentDir = d
		}
		os.MkdirAll(agentDir, 0o755)

		// Load existing models.json (may be empty/absent).
		modelsPath := filepath.Join(agentDir, "models.json")
		models := map[string]any{}
		if mdata, err := os.ReadFile(modelsPath); err == nil {
			json.Unmarshal(mdata, &models)
		}
		providers, _ := models["providers"].(map[string]any)
		if providers == nil {
			providers = map[string]any{}
		}

		// Idempotent: proxy provider already configured — nothing to do.
		if _, ok := providers["proxy"]; ok {
			return nil
		}

		// --- Source 1: ~/.claude/settings.json (backward-compatible) ---
		var settings struct {
			APIKeyHelper string            `json:"apiKeyHelper"`
			Env          map[string]string `json:"env"`
		}
		sdata, settingsErr := os.ReadFile(filepath.Join(home, ".claude", "settings.json"))
		settingsOK := settingsErr == nil && json.Unmarshal(sdata, &settings) == nil

		var baseURL, helperCmd string
		var rawHeaders string

		if settingsOK && settings.Env["ANTHROPIC_BASE_URL"] != "" {
			baseURL = settings.Env["ANTHROPIC_BASE_URL"]
			rawHeaders = settings.Env["ANTHROPIC_CUSTOM_HEADERS"]
			helperCmd = settings.APIKeyHelper
		} else {
			// --- Source 2: image-baked env var defaults + PINARD_POUR_URL ---
			baseURL = os.Getenv("PINARD_PROXY_BASE_URL")
			rawHeaders = os.Getenv("PINARD_PROXY_HEADERS")
			// helperCmd stays empty; token will be fetched via PINARD_POUR_URL below.
		}

		if baseURL == "" {
			return nil // nothing configured — skip silently
		}

		// Build the provider entry.
		headers := map[string]string{}
		if rawHeaders != "" {
			for _, part := range strings.Split(rawHeaders, ",") {
				if i := strings.Index(part, ":"); i > 0 {
					headers[strings.TrimSpace(part[:i])] = strings.TrimSpace(part[i+1:])
				}
			}
		}
		providers["proxy"] = map[string]any{
			"baseUrl":    baseURL,
			"api":        "anthropic-messages",
			"authHeader": true,
			"headers":    headers,
		}
		models["providers"] = providers
		out, _ := json.MarshalIndent(models, "", " ")
		if os.WriteFile(modelsPath, out, 0o644) == nil {
			fmt.Fprintf(os.Stderr, "[aoc] seeded proxy provider in %s\n", modelsPath)
		}

		// --- Credential: mint a token and write auth.json ---
		authPath := filepath.Join(agentDir, "auth.json")
		auth := map[string]any{}
		if adata, err := os.ReadFile(authPath); err == nil {
			json.Unmarshal(adata, &auth)
		}
		if p, ok := auth["proxy"].(map[string]any); ok && p["type"] == "oauth" {
			return nil // already have a credential
		}

		var token string
		if contractID := os.Getenv("PINARD_CAPSULE_CONTRACT"); contractID != "" {
			// Capsule path: redeem the contract token via aoc capsule-redeem.
			out, err := exec.Command("aoc", "capsule-redeem", contractID).Output()
			if err != nil {
				// Check if capsule-redeem wrote the exhaustion marker (lookup-404 or
				// signed-/do 410/404). Return an error so the caller can detect the
				// exhausted state; the babysitter turn_end handler will park the issue.
				if rd := capsuleRunDir(); rd != "" {
					if _, statErr := os.Stat(filepath.Join(rd, "capsule-exhausted.json")); statErr == nil {
						fmt.Fprintf(os.Stderr, "[aoc] capsule exhausted — marker present; agent must park\n")
						return fmt.Errorf("capsule exhausted")
					}
				}
				fmt.Fprintf(os.Stderr, "[aoc] capsule-redeem failed (proxy token not seeded): %v\n", err)
				return nil
			}
			token = strings.TrimSpace(string(out))
		} else if helperCmd != "" {
			// settings.json path: run the apiKeyHelper.
			out, err := exec.Command("sh", "-c", helperCmd).Output()
			if err != nil {
				fmt.Fprintf(os.Stderr, "[aoc] apiKeyHelper failed (proxy token not seeded): %v\n", err)
				return nil
			}
			token = strings.TrimSpace(string(out))
		} else if pourURL := os.Getenv("PINARD_POUR_URL"); pourURL != "" {
			// Baked-defaults path: fetch the per-operator token from PINARD_POUR_URL.
			token, err = fetchPourToken(pourURL)
			if err != nil {
				fmt.Fprintf(os.Stderr, "[aoc] PINARD_POUR_URL fetch failed (proxy token not seeded): %v\n", err)
				return nil
			}
		}

		if token == "" {
			return nil
		}
		auth["proxy"] = map[string]any{
			"type":    "oauth",
			"refresh": "helper",
			"access":  token,
			"expires": jwtExpMs(token),
		}
		adata, _ := json.MarshalIndent(auth, "", "  ")
		if os.WriteFile(authPath, adata, 0o600) == nil {
			fmt.Fprintf(os.Stderr, "[aoc] seeded proxy credential in %s\n", authPath)
		}
		return nil
	},
}

// fetchPourToken calls PINARD_POUR_URL and returns the bearer token string.
// The URL is expected to return either a bare token string or a JSON object
// with a "token" or "access_token" field.
func fetchPourToken(pourURL string) (string, error) {
	resp, err := http.Get(pourURL) //nolint:gosec // URL is operator-supplied
	if err != nil {
		return "", fmt.Errorf("fetch pour URL: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusGone {
		return "", fmt.Errorf("pour URL revoked (410 Gone): %s", pourURL)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("non-2xx response %d from pour URL", resp.StatusCode)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("reading pour response: %w", err)
	}
	// Try JSON first ({"token":"..."}  or {"access_token":"..."}).
	var obj map[string]any
	if json.Unmarshal(body, &obj) == nil {
		for _, key := range []string{"token", "access_token"} {
			if v, ok := obj[key].(string); ok && v != "" {
				return v, nil
			}
		}
	}
	// Fall back to bare string body.
	return strings.TrimSpace(string(body)), nil
}

// jwtExpMs returns the JWT's exp claim in epoch-millis, or now+5m if absent.
func jwtExpMs(token string) int64 {
	if parts := strings.Split(token, "."); len(parts) >= 2 {
		if payload, err := base64.RawURLEncoding.DecodeString(parts[1]); err == nil {
			var p struct {
				Exp int64 `json:"exp"`
			}
			if json.Unmarshal(payload, &p) == nil && p.Exp > 0 {
				return p.Exp * 1000
			}
		}
	}
	return time.Now().Add(5 * time.Minute).UnixMilli()
}

func init() {
	rootCmd.AddCommand(ensureProxyProviderCmd)
	resolveModelCmd.Flags().String("role", "", "Derive tier from config: worker|conductor")
	resolveModelCmd.Flags().String("project", "", "Project (for --role worker)")
	resolveModelCmd.Flags().Bool("models-list", false, "Print conductor proxy/<id>,... models list")
	rootCmd.AddCommand(resolveModelCmd)

	vigneArgsCmd.Flags().String("project", "", "Vigne/project name (required)")
	vigneArgsCmd.Flags().String("session", "", "Worker session name")
	vigneArgsCmd.Flags().String("target-branch", "main", "MR target branch")
	vigneArgsCmd.Flags().String("issue", "", "GitLab issue IID")
	vigneArgsCmd.Flags().String("parcelle", "", "Parcelle name")
	vigneArgsCmd.Flags().String("run-id", "", "Babysitter run ID")
	vigneArgsCmd.Flags().String("reviewer", "", "Reviewer username")
	vigneArgsCmd.Flags().String("assignee", "", "Assignee username")
	// Standalone (no-vignoble) mode: when --repo is given, build the args from
	// flags alone instead of reading vignes.yaml. Lets a remote worker synthesize
	// its babysitter args without the vignoble directory.
	vigneArgsCmd.Flags().String("repo", "", "Repo path (e.g. group/proj); enables vignoble-free mode")
	vigneArgsCmd.Flags().String("host", "", "GitLab host (vignoble-free mode)")
	vigneArgsCmd.Flags().String("test-strategy", "", "Test strategy (vignoble-free mode; default local)")
	vigneArgsCmd.Flags().String("test-command", "", "Test command (vignoble-free mode)")
	rootCmd.AddCommand(vigneArgsCmd)

	envExportsCmd.Flags().String("role", "", "Role context: 'conductor' emits conductor-only secrets (e.g. PINARD_OWNER_GITLAB_TOKEN); default omits them")
	rootCmd.AddCommand(envExportsCmd)
}
