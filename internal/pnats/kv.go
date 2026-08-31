package pnats

import (
	"context"
	"encoding/json"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
)

type KV struct {
	client  *Client
	buckets map[string]nats.KeyValue
	mu      sync.Mutex
}

func NewKV(client *Client) *KV {
	return &KV{
		client:  client,
		buckets: make(map[string]nats.KeyValue),
	}
}

func (k *KV) getBucket(name string) (nats.KeyValue, error) {
	k.mu.Lock()
	defer k.mu.Unlock()

	if kv, ok := k.buckets[name]; ok {
		return kv, nil
	}

	if err := k.client.Connect(); err != nil {
		return nil, err
	}

	kv, err := k.client.JS().KeyValue(name)
	if err != nil {
		return nil, err
	}
	k.buckets[name] = kv
	return kv, nil
}

// EnsureBucket creates the KV bucket if it does not already exist, and caches
// the handle. Safe to call repeatedly.
func (k *KV) EnsureBucket(name string) error {
	k.mu.Lock()
	defer k.mu.Unlock()
	if _, ok := k.buckets[name]; ok {
		return nil
	}
	if err := k.client.Connect(); err != nil {
		return err
	}
	js := k.client.JS()
	kv, err := js.KeyValue(name)
	if err != nil {
		kv, err = js.CreateKeyValue(&nats.KeyValueConfig{Bucket: name})
		if err != nil {
			return err
		}
	}
	k.buckets[name] = kv
	return nil
}

func (k *KV) Get(bucket, key string) (map[string]any, error) {
	kv, err := k.getBucket(bucket)
	if err != nil {
		return nil, err
	}
	entry, err := kv.Get(key)
	if err != nil {
		return nil, err
	}
	var result map[string]any
	if err := json.Unmarshal(entry.Value(), &result); err != nil {
		return nil, err
	}
	return result, nil
}

func (k *KV) Put(bucket, key string, value any) error {
	kv, err := k.getBucket(bucket)
	if err != nil {
		return err
	}
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	_, err = kv.Put(key, data)
	return err
}

func (k *KV) Del(bucket, key string) error {
	kv, err := k.getBucket(bucket)
	if err != nil {
		return err
	}
	return kv.Delete(key)
}

// Keys lists the keys in a KV bucket.
//
// It deliberately does NOT use nats.go's KeyValue.Keys()/ListKeys(), which both
// enumerate via a WatchAll consumer/watcher. That watcher path was observed to
// fail deterministically for the webterm gateway over WSS — returning an empty
// set while direct Get worked fine — surfacing as a spurious "not an operator"
// 403 (issue #115). Instead we read the bucket's stream subjects via StreamInfo,
// a plain request/reply JS API call (same reliability class as Get), and derive
// the keys from the `$KV.<bucket>.<key>` subjects.
func (k *KV) Keys(bucket string) ([]string, error) {
	if err := k.client.Connect(); err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	prefix := "$KV." + bucket + "."
	si, err := k.client.JS().StreamInfo(
		"KV_"+bucket,
		&nats.StreamInfoRequest{SubjectsFilter: prefix + ">"},
		nats.Context(ctx),
	)
	if err != nil {
		return nil, err
	}
	var keys []string
	for subject := range si.State.Subjects {
		if key := strings.TrimPrefix(subject, prefix); key != subject && key != "" {
			keys = append(keys, key)
		}
	}
	return keys, nil
}
