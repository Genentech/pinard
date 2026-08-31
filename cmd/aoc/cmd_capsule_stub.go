//go:build !capsule

package main

// capsuleRunDir returns "" in non-capsule builds (no capsule-redeem command).
func capsuleRunDir() string { return "" }
