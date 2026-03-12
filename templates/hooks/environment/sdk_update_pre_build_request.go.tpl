	// Don't attempt updates while the environment is in a transitional state.
	// MWAA rejects updates unless the environment is AVAILABLE.
	if latest.ko.Status.Status != nil {
		status := *latest.ko.Status.Status
		if status != string(svcsdktypes.EnvironmentStatusAvailable) {
			return nil, ackrequeue.NeededAfter(
				fmt.Errorf("environment is in %s state, cannot update until AVAILABLE", status),
				ackrequeue.DefaultRequeueAfterDuration,
			)
		}
	}
