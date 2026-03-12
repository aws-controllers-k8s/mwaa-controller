	// AirflowConfigurationOptions: GetEnvironment returns redacted values
	// ("***") for sensitive config options. Preserve the desired values to
	// prevent false drift detection and infinite update loops.
	if r.ko.Spec.AirflowConfigurationOptions != nil {
		ko.Spec.AirflowConfigurationOptions = r.ko.Spec.AirflowConfigurationOptions
	}

	// Return terminal error for failure states so updateConditions preserves it.
	// NOTE: UPDATE_FAILED is intentionally excluded — MWAA rolls back to the
	// previous configuration and the environment returns to AVAILABLE, so the
	// controller should requeue and let the user fix their spec.
	if ko.Status.Status != nil {
		status := *ko.Status.Status
		if status == string(svcsdktypes.EnvironmentStatusCreateFailed) ||
			status == string(svcsdktypes.EnvironmentStatusUnavailable) {
			return &resource{ko}, ackerr.NewTerminalError(
				fmt.Errorf("environment is in terminal state: %s", status),
			)
		}
	}
