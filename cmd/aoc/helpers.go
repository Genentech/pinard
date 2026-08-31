package main

import (
	"os"
	"path/filepath"
)

func selfPath() string {
	exe, err := os.Executable()
	if err != nil {
		return "aoc"
	}
	resolved, err := filepath.EvalSymlinks(exe)
	if err != nil {
		return exe
	}
	return resolved
}
