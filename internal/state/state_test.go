package state

import (
	"os"
	"path/filepath"
	"sync"
	"testing"
)

type testData struct {
	Name  string `yaml:"name"`
	Count int    `yaml:"count"`
}

func TestLoadAndUpdate(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "test.yaml")

	s, err := Load[testData](path)
	if err != nil {
		t.Fatal(err)
	}

	// Initial state is zero value
	if s.Data.Name != "" {
		t.Errorf("expected empty name, got %q", s.Data.Name)
	}

	// Update writes through to disk
	err = s.Update(func(d *testData) {
		d.Name = "hello"
		d.Count = 42
	})
	if err != nil {
		t.Fatal(err)
	}

	// Verify file was written
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(data) == 0 {
		t.Fatal("file is empty after update")
	}

	// Load again — should have the data
	s2, err := Load[testData](path)
	if err != nil {
		t.Fatal(err)
	}
	if s2.Data.Name != "hello" || s2.Data.Count != 42 {
		t.Errorf("got %+v, want {hello 42}", s2.Data)
	}
}

func TestConcurrentUpdates(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "concurrent.yaml")

	s, _ := Load[testData](path)

	var wg sync.WaitGroup
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			s.Update(func(d *testData) {
				d.Count++
			})
		}()
	}
	wg.Wait()

	if s.Data.Count != 100 {
		t.Errorf("expected 100, got %d", s.Data.Count)
	}
}

func TestLoadNonExistent(t *testing.T) {
	s, err := Load[testData]("/nonexistent/path.yaml")
	if err != nil {
		t.Fatal(err)
	}
	if s.Data.Name != "" {
		t.Error("expected zero value for non-existent file")
	}
}

// BUG REGRESSION: Two independent Store instances on the same file (simulates
// daemon + aoc track-mr). Without reload-before-update, the daemon's in-memory
// state overwrites changes made by the other process.
func TestCrossProcessUpdateNotOverwritten(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "shared.yaml")

	// Process A: daemon
	daemon, _ := Load[testData](path)
	daemon.Update(func(d *testData) {
		d.Name = "daemon"
		d.Count = 1
	})

	// Process B: aoc track-mr writes to the same file
	trackMr, _ := Load[testData](path)
	trackMr.Update(func(d *testData) {
		d.Name = "track-mr"
		d.Count = 99
	})

	// Process A updates again — must see process B's changes via reload()
	daemon.Update(func(d *testData) {
		// Without reload, d.Name would still be "daemon" and Count=1,
		// and this flush would overwrite process B's data.
		d.Count = d.Count + 1
	})

	// Verify: process B's name should survive, count should be 100
	verify, _ := Load[testData](path)
	if verify.Data.Name != "track-mr" {
		t.Errorf("name should be 'track-mr' (from process B), got %q — cross-process state was overwritten", verify.Data.Name)
	}
	if verify.Data.Count != 100 {
		t.Errorf("count should be 100 (99+1), got %d — reload-before-update is broken", verify.Data.Count)
	}
}

// BUG REGRESSION: Read() must also reload from disk before returning data,
// otherwise stale in-memory state is returned.
func TestReadReloadsFromDisk(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "read-reload.yaml")

	s1, _ := Load[testData](path)
	s1.Update(func(d *testData) {
		d.Name = "original"
	})

	// External process writes different data
	s2, _ := Load[testData](path)
	s2.Update(func(d *testData) {
		d.Name = "external-update"
	})

	// s1.Read() must see the external change
	var name string
	s1.Read(func(d *testData) {
		name = d.Name
	})
	if name != "external-update" {
		t.Errorf("Read() should see external changes, got %q", name)
	}
}
