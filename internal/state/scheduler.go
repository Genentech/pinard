package state

type SchedulerRuns struct {
	Runs map[string]string `yaml:",inline"`
}
