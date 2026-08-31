package session

type Manager interface {
	SpawnWorker(workspace, name, command string) error
	StopWorker(workspace, name string) error
	GetWorkerCwd(workspace, name string) (string, error)
	Close() error
}

// New returns a tmux-based session manager using per-vignoble sockets.
func New() Manager {
	return newTmux()
}
