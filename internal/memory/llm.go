package memory

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"golang.org/x/oauth2/google"
)

// LLMAuthError is raised when the LLM token is missing, expired, or rejected.
type LLMAuthError struct {
	Message string
}

func (e *LLMAuthError) Error() string { return "llm auth: " + e.Message }

// LLMError is raised on non-auth LLM failures.
type LLMError struct {
	Message string
}

func (e *LLMError) Error() string { return "llm: " + e.Message }

// LLMMessage is a single chat message.
type LLMMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// LLMClient is a model-agnostic HTTP LLM client.
//
// Environment variables (read via BuildLLMClient):
//
//	MEMORY_LLM_API      — "openai-chat" | "anthropic-messages" (default: anthropic-messages)
//	MEMORY_LLM_BASE_URL — endpoint override
//	MEMORY_LLM_MODEL    — model id
//	MEMORY_LLM_AUTH     — "google-sa" | "url" | "static-key" (auto-detect when unset)
//	MEMORY_TOKEN_URL    — pour-token URL (for "url" auth)
//	ANTHROPIC_API_KEY   — static key for anthropic-messages
//	OPENAI_API_KEY      — static key for openai-chat
type LLMClient struct {
	API     string
	Model   string
	BaseURL string
	hc      *http.Client
	tp      tokenProvider
}

type tokenProvider interface {
	GetToken() (string, error)
}

// ── Token providers ───────────────────────────────────────────────────────────

type staticKeyProvider struct{ key string }

func (p *staticKeyProvider) GetToken() (string, error) {
	if p.key == "" {
		return "", &LLMAuthError{Message: "no static API key configured (set ANTHROPIC_API_KEY, OPENAI_API_KEY, or MEMORY_TOKEN_URL)"}
	}
	return p.key, nil
}

type urlTokenProvider struct {
	url string
	hc  *http.Client
}

func (p *urlTokenProvider) GetToken() (string, error) {
	resp, err := p.hc.Get(p.url)
	if err != nil {
		return "", &LLMAuthError{Message: fmt.Sprintf("token URL fetch failed: %v", err)}
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		return "", &LLMAuthError{Message: fmt.Sprintf("token URL HTTP %d", resp.StatusCode)}
	}
	ct := resp.Header.Get("Content-Type")
	if strings.Contains(ct, "application/json") {
		var obj map[string]any
		if jsonErr := json.Unmarshal(body, &obj); jsonErr == nil {
			for _, k := range []string{"api_key", "token"} {
				if v, ok := obj[k].(string); ok && v != "" {
					return v, nil
				}
			}
		}
	}
	key := strings.TrimSpace(string(body))
	if key == "" {
		return "", &LLMAuthError{Message: "token URL returned empty key"}
	}
	return key, nil
}

type googleSAProvider struct {
	saPath  string
	mu      sync.Mutex
	token   string
	expires time.Time
}

// GetToken mints an OAuth2 access token from the Service Account JSON at
// p.saPath (GOOGLE_APPLICATION_CREDENTIALS). It uses golang.org/x/oauth2/google
// so no external gcloud binary is required — safe in debian:bookworm-slim images.
func (p *googleSAProvider) GetToken() (string, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.token != "" && time.Now().Before(p.expires.Add(-5*time.Minute)) {
		return p.token, nil
	}
	saJSON, err := os.ReadFile(p.saPath)
	if err != nil {
		return "", &LLMAuthError{Message: fmt.Sprintf("google-sa: read credentials file %q: %v", p.saPath, err)}
	}
	creds, err := google.CredentialsFromJSON(
		context.Background(),
		saJSON,
		"https://www.googleapis.com/auth/cloud-platform",
	)
	if err != nil {
		return "", &LLMAuthError{Message: fmt.Sprintf("google-sa: parse credentials: %v", err)}
	}
	t, err := creds.TokenSource.Token()
	if err != nil {
		return "", &LLMAuthError{Message: fmt.Sprintf("google-sa: token mint failed: %v", err)}
	}
	if t.AccessToken == "" {
		return "", &LLMAuthError{Message: "google-sa: token source returned empty access token"}
	}
	p.token = t.AccessToken
	if !t.Expiry.IsZero() {
		p.expires = t.Expiry
	} else {
		p.expires = time.Now().Add(50 * time.Minute)
	}
	return p.token, nil
}

// ── BuildLLMClient factory ────────────────────────────────────────────────────

// BuildLLMClient creates an LLMClient from environment variables.
func BuildLLMClient() (*LLMClient, error) {
	api := envOr("MEMORY_LLM_API", "anthropic-messages")
	baseURL := os.Getenv("MEMORY_LLM_BASE_URL")
	model := os.Getenv("MEMORY_LLM_MODEL")
	if model == "" {
		if api == "anthropic-messages" {
			model = "claude-haiku-4-5-20251001"
		} else {
			model = "gemini-2.0-flash"
		}
	}

	hc := &http.Client{Timeout: 60 * time.Second}
	tp, err := resolveTokenProvider(api, hc)
	if err != nil {
		return nil, err
	}

	return &LLMClient{
		API:     api,
		Model:   model,
		BaseURL: baseURL,
		hc:      hc,
		tp:      tp,
	}, nil
}

func resolveTokenProvider(api string, hc *http.Client) (tokenProvider, error) {
	auth := os.Getenv("MEMORY_LLM_AUTH")

	switch auth {
	case "google-sa":
		saPath := os.Getenv("GOOGLE_APPLICATION_CREDENTIALS")
		if saPath == "" {
			return nil, fmt.Errorf("MEMORY_LLM_AUTH=google-sa requires GOOGLE_APPLICATION_CREDENTIALS")
		}
		return &googleSAProvider{saPath: saPath}, nil
	case "url":
		u := os.Getenv("MEMORY_TOKEN_URL")
		if u == "" {
			return nil, fmt.Errorf("MEMORY_LLM_AUTH=url requires MEMORY_TOKEN_URL")
		}
		return &urlTokenProvider{url: u, hc: hc}, nil
	case "static-key":
		return &staticKeyProvider{key: staticKeyForAPI(api)}, nil
	}

	// Auto-detect.
	if u := os.Getenv("MEMORY_TOKEN_URL"); u != "" {
		return &urlTokenProvider{url: u, hc: hc}, nil
	}
	return &staticKeyProvider{key: staticKeyForAPI(api)}, nil
}

func staticKeyForAPI(api string) string {
	if api == "anthropic-messages" {
		return os.Getenv("ANTHROPIC_API_KEY")
	}
	return os.Getenv("OPENAI_API_KEY")
}

// ── Probe ─────────────────────────────────────────────────────────────────────

// Probe sends a minimal request to validate connectivity and auth.
func (c *LLMClient) Probe() error {
	tok, err := c.tp.GetToken()
	if err != nil {
		return err
	}
	if c.API == "anthropic-messages" {
		return c.probeAnthropic(tok)
	}
	return c.probeOpenAI(tok)
}

func (c *LLMClient) probeAnthropic(tok string) error {
	base := c.resolvedBaseURL("https://api.anthropic.com")
	payload := map[string]any{
		"model": c.Model, "max_tokens": 1,
		"messages": []map[string]any{{"role": "user", "content": "ping"}},
	}
	body, _ := json.Marshal(payload)
	req, _ := http.NewRequest(http.MethodPost, base+"/v1/messages", bytes.NewReader(body))
	req.Header.Set("x-api-key", tok)
	req.Header.Set("anthropic-version", "2023-06-01")
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.hc.Do(req)
	if err != nil {
		return &LLMError{Message: fmt.Sprintf("Anthropic unreachable: %v", err)}
	}
	defer resp.Body.Close()
	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		return &LLMAuthError{Message: fmt.Sprintf("LLM token rejected (HTTP %d)", resp.StatusCode)}
	}
	return nil
}

func (c *LLMClient) probeOpenAI(tok string) error {
	payload := map[string]any{
		"model": c.Model, "max_tokens": 1,
		"messages": []map[string]any{{"role": "user", "content": "ping"}},
	}
	body, _ := json.Marshal(payload)
	req, _ := http.NewRequest(http.MethodPost, c.openAIChatURL(), bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+tok)
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.hc.Do(req)
	if err != nil {
		return &LLMError{Message: fmt.Sprintf("OpenAI-compat unreachable: %v", err)}
	}
	defer resp.Body.Close()
	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		return &LLMAuthError{Message: fmt.Sprintf("LLM token rejected (HTTP %d)", resp.StatusCode)}
	}
	return nil
}

// ── Complete ──────────────────────────────────────────────────────────────────

// Complete sends a chat completion request and returns the response text.
func (c *LLMClient) Complete(messages []LLMMessage, maxTokens int, system string) (string, error) {
	tok, err := c.tp.GetToken()
	if err != nil {
		return "", err
	}
	if c.API == "anthropic-messages" {
		return c.completeAnthropic(tok, messages, maxTokens, system)
	}
	return c.completeOpenAI(tok, messages, maxTokens, system)
}

func (c *LLMClient) completeAnthropic(tok string, messages []LLMMessage, maxTokens int, system string) (string, error) {
	base := c.resolvedBaseURL("https://api.anthropic.com")
	payload := map[string]any{
		"model":      c.Model,
		"max_tokens": maxTokens,
		"messages":   messages,
	}
	if system != "" {
		payload["system"] = system
	}
	body, _ := json.Marshal(payload)

	req, _ := http.NewRequest(http.MethodPost, base+"/v1/messages", bytes.NewReader(body))
	req.Header.Set("x-api-key", tok)
	req.Header.Set("anthropic-version", "2023-06-01")
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.hc.Do(req)
	if err != nil {
		return "", &LLMError{Message: fmt.Sprintf("Anthropic request failed: %v", err)}
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)

	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		return "", &LLMAuthError{Message: fmt.Sprintf("HTTP %d", resp.StatusCode)}
	}
	if resp.StatusCode != http.StatusOK {
		return "", &LLMError{Message: fmt.Sprintf("Anthropic HTTP %d: %s", resp.StatusCode, truncate(string(respBody), 300))}
	}

	var result struct {
		Content []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
	}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return "", &LLMError{Message: fmt.Sprintf("parse Anthropic response: %v", err)}
	}
	for _, part := range result.Content {
		if part.Type == "text" {
			return part.Text, nil
		}
	}
	return "", nil
}

func (c *LLMClient) completeOpenAI(tok string, messages []LLMMessage, maxTokens int, system string) (string, error) {
	allMessages := messages
	if system != "" {
		allMessages = append([]LLMMessage{{Role: "system", Content: system}}, messages...)
	}

	payload := map[string]any{
		"model":      c.Model,
		"max_tokens": maxTokens,
		"messages":   allMessages,
	}
	body, _ := json.Marshal(payload)

	req, _ := http.NewRequest(http.MethodPost, c.openAIChatURL(), bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+tok)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.hc.Do(req)
	if err != nil {
		return "", &LLMError{Message: fmt.Sprintf("OpenAI request failed: %v", err)}
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)

	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		return "", &LLMAuthError{Message: fmt.Sprintf("HTTP %d", resp.StatusCode)}
	}
	if resp.StatusCode != http.StatusOK {
		return "", &LLMError{Message: fmt.Sprintf("OpenAI HTTP %d: %s", resp.StatusCode, truncate(string(respBody), 300))}
	}

	var result struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return "", &LLMError{Message: fmt.Sprintf("parse OpenAI response: %v", err)}
	}
	if len(result.Choices) == 0 {
		return "", nil
	}
	return result.Choices[0].Message.Content, nil
}

func (c *LLMClient) resolvedBaseURL(defaultBase string) string {
	if c.BaseURL != "" {
		return strings.TrimRight(c.BaseURL, "/")
	}
	return defaultBase
}

// openAIChatURL composes the OpenAI-compat chat/completions endpoint.
//
// The OpenAI SDK treats base_url as the full API root (already including the
// version segment) and appends "/chat/completions". We mirror that: the built-in
// default carries "/v1", and an override MEMORY_LLM_BASE_URL is used verbatim
// with only "/chat/completions" appended. This avoids the double "/v1" that
// produced HTTP 404 against a Vertex ".../endpoints/openapi" base (#264).
func (c *LLMClient) openAIChatURL() string {
	return c.resolvedBaseURL("https://api.openai.com/v1") + "/chat/completions"
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
