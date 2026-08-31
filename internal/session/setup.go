package session

// EnsureWorkspaces is a no-op for tmux (sockets are created lazily on spawn).
func EnsureWorkspaces(names []string) {}
