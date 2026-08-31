package pnats

// KVReader is the interface used by watchers to read KV state.
// Implemented by *KV for production; mockable for tests.
type KVReader interface {
	Get(bucket, key string) (map[string]any, error)
	Keys(bucket string) ([]string, error)
}

// KVWriter extends KVReader with write operations.
type KVWriter interface {
	KVReader
	Put(bucket, key string, value any) error
	Del(bucket, key string) error
}
