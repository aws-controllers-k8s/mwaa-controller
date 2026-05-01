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

	// Tags are managed via TagResource/UntagResource, not UpdateEnvironment.
	if delta.DifferentAt("Spec.Tags") {
		if err := syncTags(
			ctx, rm.sdkapi, rm.metrics,
			string(*latest.ko.Status.ACKResourceMetadata.ARN),
			aws.ToStringMap(desired.ko.Spec.Tags), aws.ToStringMap(latest.ko.Spec.Tags),
		); err != nil {
			return nil, err
		}
	}
	if !delta.DifferentExcept("Spec.Tags") {
		return desired, nil
	}
