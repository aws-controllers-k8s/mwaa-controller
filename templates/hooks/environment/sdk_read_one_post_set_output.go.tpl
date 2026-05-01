	// AirflowConfigurationOptions: GetEnvironment returns redacted values
	// ("***") for values MWAA considers sensitive (e.g. secrets.backend_kwargs,
	// core.fernet_key, core.sql_alchemy_conn). Copy the stored desired values
	// onto the observed resource so the delta comparator does not see the
	// redacted "***" as a diff, which would trigger an infinite update loop.
	//
	// This does NOT disable spec-change detection: when the user patches the
	// CR, the reconciler compares the new desired spec against `latest` (which
	// carries the previously-stored desired values via this copy), sees a real
	// diff, and calls UpdateEnvironment. User-initiated updates work correctly.
	//
	// Limitation: we cannot detect drift caused by out-of-band edits to
	// sensitive keys (e.g. changes made in the MWAA console). There is no
	// unredacted read path in the MWAA API. Terraform handles this the same
	// way (field marked Sensitive, diff suppressed). For values that must stay
	// in sync, use the AWS Secrets Manager backend for Airflow:
	// https://docs.aws.amazon.com/mwaa/latest/userguide/connections-secrets-manager.html
	if r.ko.Spec.AirflowConfigurationOptions != nil {
		ko.Spec.AirflowConfigurationOptions = r.ko.Spec.AirflowConfigurationOptions
	}

	// Return terminal error for failure states so updateConditions preserves it.
	if ko.Status.Status != nil {
		status := *ko.Status.Status
		if status == string(svcsdktypes.EnvironmentStatusCreateFailed) ||
			status == string(svcsdktypes.EnvironmentStatusUnavailable) {
			return &resource{ko}, ackerr.NewTerminalError(
				fmt.Errorf("environment is in terminal state: %s", status),
			)
		}
	}

	// Surface UPDATE_FAILED to the user.
	//
	// MWAA rolls back after a failed update and the environment returns to
	// AVAILABLE, but Status.LastUpdate.Status stays "FAILED" and
	// Status.LastUpdate.Error carries the reason. Without this, the user
	// has no signal that their patch silently failed — the CR would simply
	// show ACK.ResourceSynced=True with stale config.
	//
	// Explicitly set ACK.ResourceSynced=False with the failure details in
	// the condition's `message` field; the runtime's ensureConditions guard
	// (`if ackcondition.Synced(res) == nil`) preserves this instead of
	// overwriting with the generic "Unable to determine..." Unknown status
	// it would otherwise assign for a non-terminal requeue error.
	//
	// Using a non-terminal requeue error (not NewTerminalError) so that:
	//   - The requeue timer still drives re-polling of GetEnvironment
	//   - The next user CR patch triggers a new Update attempt; on success,
	//     MWAA overwrites LastUpdate.Status to SUCCESS and this branch no
	//     longer fires, clearing the condition automatically.
	if res, err := handleUpdateFailed(&resource{ko}); err != nil {
		return res, err
	}
