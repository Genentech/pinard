package state

import (
	"os"
	"path/filepath"
	"sync"

	"gopkg.in/yaml.v3"
)

type Store[T any] struct {
	mu   sync.Mutex
	path string
	Data T
}

func Load[T any](path string) (*Store[T], error) {
	s := &Store[T]{path: path}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return s, nil
		}
		return nil, err
	}
	if err := yaml.Unmarshal(data, &s.Data); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *Store[T]) Update(fn func(*T)) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.reload()
	fn(&s.Data)
	return s.flush()
}

func (s *Store[T]) Read(fn func(*T)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.reload()
	fn(&s.Data)
}

func (s *Store[T]) flush() error {
	data, err := yaml.Marshal(&s.Data)
	if err != nil {
		return err
	}

	dir := filepath.Dir(s.path)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return err
	}

	// File lock to prevent concurrent writes from daemon + track-mr
	lockPath := s.path + ".lock"
	lock, err := os.OpenFile(lockPath, os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err := flock(lock); err != nil {
		return err
	}
	defer funlock(lock)

	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0644); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

func (s *Store[T]) reload() {
	// Also take lock for consistent reads
	lockPath := s.path + ".lock"
	lock, err := os.OpenFile(lockPath, os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		data, err := os.ReadFile(s.path)
		if err != nil {
			return
		}
		yaml.Unmarshal(data, &s.Data)
		return
	}
	defer lock.Close()
	flock(lock)
	defer funlock(lock)

	data, err := os.ReadFile(s.path)
	if err != nil {
		return
	}
	yaml.Unmarshal(data, &s.Data)
}
