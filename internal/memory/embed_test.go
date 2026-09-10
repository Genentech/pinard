package memory

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestEmbedderEmbed(t *testing.T) {
	vec := make([]float64, EmbeddingDim)
	for i := range vec {
		vec[i] = float64(i) / float64(EmbeddingDim)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		resp := map[string]any{
			"data": []map[string]any{
				{"index": 0, "embedding": vec},
			},
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	}))
	defer srv.Close()

	emb := &Embedder{URL: srv.URL, Model: "test-model", hc: &http.Client{}}
	result, err := emb.Embed("hello world")
	if err != nil {
		t.Fatal(err)
	}
	if len(result) != EmbeddingDim {
		t.Errorf("embedding dim = %d, want %d", len(result), EmbeddingDim)
	}
}

func TestEmbedderBatchEmpty(t *testing.T) {
	emb := &Embedder{URL: "http://unused", Model: "test-model", hc: &http.Client{}}
	result, err := emb.EmbedBatch(nil)
	if err != nil {
		t.Fatal(err)
	}
	if result != nil {
		t.Error("expected nil for empty batch")
	}
}

func TestEmbedderHTTPError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "service unavailable", 503)
	}))
	defer srv.Close()

	emb := &Embedder{URL: srv.URL, Model: "test-model", hc: &http.Client{}}
	_, err := emb.Embed("test")
	if err == nil {
		t.Fatal("expected error")
	}
}

func TestEmbedderWrongDim(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		resp := map[string]any{
			"data": []map[string]any{
				{"index": 0, "embedding": []float64{0.1, 0.2, 0.3}}, // wrong dim
			},
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	}))
	defer srv.Close()

	emb := &Embedder{URL: srv.URL, Model: "m", hc: &http.Client{}}
	_, err := emb.Embed("test")
	if err == nil {
		t.Fatal("expected error for wrong dim")
	}
}
