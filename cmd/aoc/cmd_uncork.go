package main

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"
)

// uncorkManifest is the JSON shape returned by PINARD_UNCORK_URL.
type uncorkManifest struct {
	Version int           `json:"version"`
	Files   []uncorkFile  `json:"files"`
}

type uncorkFile struct {
	Path     string `json:"path"`
	Mode     string `json:"mode"`
	Content  string `json:"content"`
	Encoding string `json:"encoding"`
	Checksum string `json:"checksum"` // optional sha256:<hex>
}

// aoc uncork — materialize a credential/config bundle for a sandboxed worker.
// Reads a JSON manifest from --url (default $PINARD_UNCORK_URL) or stdin,
// and writes each listed file under $HOME.
var uncorkCmd = &cobra.Command{
	Use:   "uncork",
	Short: "Materialize a credential bundle from a URL or stdin into $HOME",
	RunE: func(cmd *cobra.Command, args []string) error {
		rawURL, _ := cmd.Flags().GetString("url")
		homeDir, _ := cmd.Flags().GetString("home")

		if rawURL == "" {
			rawURL = os.Getenv("PINARD_UNCORK_URL")
		}
		if homeDir == "" {
			var err error
			homeDir, err = os.UserHomeDir()
			if err != nil {
				return fmt.Errorf("cannot determine home dir: %w", err)
			}
		}

		var data []byte
		if rawURL != "" {
			resp, err := http.Get(rawURL) //nolint:gosec // URL is operator-supplied
			if err != nil {
				return fmt.Errorf("uncork: fetch %s: %w", rawURL, err)
			}
			defer resp.Body.Close()
			if resp.StatusCode == http.StatusGone {
				return fmt.Errorf("uncork: URL has been revoked (410 Gone): %s", rawURL)
			}
			if resp.StatusCode < 200 || resp.StatusCode >= 300 {
				return fmt.Errorf("uncork: non-2xx response %d from %s", resp.StatusCode, rawURL)
			}
			data, err = io.ReadAll(resp.Body)
			if err != nil {
				return fmt.Errorf("uncork: reading response: %w", err)
			}
		} else {
			var err error
			data, err = io.ReadAll(os.Stdin)
			if err != nil {
				return fmt.Errorf("uncork: reading stdin: %w", err)
			}
		}

		var manifest uncorkManifest
		if err := json.Unmarshal(data, &manifest); err != nil {
			return fmt.Errorf("uncork: malformed manifest JSON: %w", err)
		}
		if len(manifest.Files) == 0 {
			return fmt.Errorf("uncork: manifest contains no files")
		}

		for _, f := range manifest.Files {
			if err := uncorkWriteFile(homeDir, f); err != nil {
				return err
			}
		}
		return nil
	},
}

func uncorkWriteFile(homeDir string, f uncorkFile) error {
	if f.Path == "" {
		return fmt.Errorf("uncork: file entry has empty path")
	}
	// Reject absolute paths and path traversal.
	if filepath.IsAbs(f.Path) {
		return fmt.Errorf("uncork: absolute path rejected: %s", f.Path)
	}
	cleaned := filepath.Clean(f.Path)
	if strings.HasPrefix(cleaned, "..") {
		return fmt.Errorf("uncork: path traversal rejected: %s", f.Path)
	}

	dest := filepath.Join(homeDir, cleaned)
	// Double-check the resolved path stays inside homeDir.
	if !strings.HasPrefix(dest, homeDir+string(os.PathSeparator)) && dest != homeDir {
		return fmt.Errorf("uncork: path escapes home dir: %s", f.Path)
	}

	var content []byte
	if f.Encoding == "base64" {
		dec, err := base64.StdEncoding.DecodeString(f.Content)
		if err != nil {
			return fmt.Errorf("uncork: base64 decode %s: %w", f.Path, err)
		}
		content = dec
	} else {
		content = []byte(f.Content)
	}

	// Optional checksum verification (sha256:<hex>).
	if f.Checksum != "" {
		if !strings.HasPrefix(f.Checksum, "sha256:") {
			return fmt.Errorf("uncork: unsupported checksum format for %s: %s", f.Path, f.Checksum)
		}
		want := strings.TrimPrefix(f.Checksum, "sha256:")
		sum := sha256.Sum256(content)
		got := hex.EncodeToString(sum[:])
		if got != want {
			return fmt.Errorf("uncork: checksum mismatch for %s: got %s want %s", f.Path, got, want)
		}
	}

	mode := os.FileMode(0o600)
	if f.Mode != "" {
		var m uint32
		if _, err := fmt.Sscanf(f.Mode, "%o", &m); err != nil {
			return fmt.Errorf("uncork: invalid mode %q for %s: %w", f.Mode, f.Path, err)
		}
		mode = os.FileMode(m)
	}

	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return fmt.Errorf("uncork: mkdir for %s: %w", f.Path, err)
	}
	if err := os.WriteFile(dest, content, mode); err != nil {
		return fmt.Errorf("uncork: write %s: %w", f.Path, err)
	}
	fmt.Fprintf(os.Stderr, "[aoc uncork] wrote %s (mode %04o)\n", dest, mode)
	return nil
}

func init() {
	uncorkCmd.Flags().String("url", "", "Manifest URL (default $PINARD_UNCORK_URL)")
	uncorkCmd.Flags().String("home", "", "Base directory for file writes (default $HOME)")
	rootCmd.AddCommand(uncorkCmd)
}
