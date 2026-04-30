package environment

import (
	"fmt"

	ackcompare "github.com/aws-controllers-k8s/runtime/pkg/compare"
	ackcondition "github.com/aws-controllers-k8s/runtime/pkg/condition"
	ackrequeue "github.com/aws-controllers-k8s/runtime/pkg/requeue"
	svcsdktypes "github.com/aws/aws-sdk-go-v2/service/mwaa/types"
	corev1 "k8s.io/api/core/v1"

	"github.com/aws-controllers-k8s/mwaa-controller/pkg/tags"
)

var syncTags = tags.SyncTags

// customPreCompare performs field comparisons that must not log the
// compared values. It is called from the generated newResourceDelta via
// the delta_pre_compare hook configured in generator.yaml.
//
// AirflowConfigurationOptions is a map<string,string> where users commonly
// place secrets (e.g. core.fernet_key, core.sql_alchemy_conn,
// secrets.backend_kwargs — see also the sensitive-value handling in
// templates/hooks/environment/sdk_read_one_post_set_output.go.tpl). The
// ACK runtime reconciler logs delta.Differences at Info level on every
// detected spec change (runtime/pkg/runtime/reconciler.go), which includes
// the A/B values attached to each Difference. If the default generated
// comparator handled this field it would leak those secrets into the
// controller logs.
//
// We reproduce the generated map[string]*string comparison logic here but
// pass nil placeholders to delta.Add so the logged Differences only show
// the path ("Spec.AirflowConfigurationOptions"), not the values.
// Delta.DifferentAt and Delta.DifferentExcept in
// runtime/pkg/compare/delta.go inspect Path only — they never dereference
// A/B — so scrubbing the values does not change control flow. The field
// is marked compare.is_ignored in generator.yaml so the default generated
// comparison block is omitted.
func customPreCompare(delta *ackcompare.Delta, a, b *resource) {
	if len(a.ko.Spec.AirflowConfigurationOptions) != len(b.ko.Spec.AirflowConfigurationOptions) {
		delta.Add("Spec.AirflowConfigurationOptions", nil, nil)
	} else if len(a.ko.Spec.AirflowConfigurationOptions) > 0 {
		if !ackcompare.MapStringStringPEqual(
			a.ko.Spec.AirflowConfigurationOptions,
			b.ko.Spec.AirflowConfigurationOptions,
		) {
			delta.Add("Spec.AirflowConfigurationOptions", nil, nil)
		}
	}
}

// handleUpdateFailed inspects the resource's Status.LastUpdate and, if MWAA
// reported a failed update, sets ACK.ResourceSynced=False on the resource
// with the MWAA error details and returns a non-terminal requeue error so
// the condition is refreshed on future reconciles.
//
// MWAA rolls back after a failed update and the environment returns to
// AVAILABLE, but Status.LastUpdate.Status stays "FAILED" and
// Status.LastUpdate.Error carries the reason. Surfacing this as
// ACK.ResourceSynced=False is the only signal the user has that their patch
// silently failed — otherwise the CR would appear Synced with stale config.
//
// Using a non-terminal requeue error (not a terminal error) so that:
//   - The requeue timer still drives re-polling of GetEnvironment.
//   - The next user CR patch triggers a new Update attempt; on success,
//     MWAA overwrites LastUpdate.Status to SUCCESS and this branch no
//     longer fires, clearing the condition automatically.
//
// Returns (nil, nil) when the resource is not in a failed-update state and
// the caller should proceed normally. Returns (r, requeueErr) when the
// failed state is detected.
func handleUpdateFailed(r *resource) (*resource, error) {
	ko := r.ko
	if ko.Status.LastUpdate == nil ||
		ko.Status.LastUpdate.Status == nil ||
		*ko.Status.LastUpdate.Status != string(svcsdktypes.UpdateStatusFailed) {
		return nil, nil
	}
	code := "Unknown"
	msg := "update failed with no error details"
	if ko.Status.LastUpdate.Error != nil {
		if ko.Status.LastUpdate.Error.ErrorCode != nil {
			code = *ko.Status.LastUpdate.Error.ErrorCode
		}
		if ko.Status.LastUpdate.Error.ErrorMessage != nil {
			msg = *ko.Status.LastUpdate.Error.ErrorMessage
		}
	}
	errMsg := fmt.Sprintf("last UpdateEnvironment failed: %s: %s; patch the spec to retry", code, msg)
	ackcondition.SetSynced(r, corev1.ConditionFalse, &errMsg, nil)
	return r, ackrequeue.NeededAfter(
		fmt.Errorf("%s", errMsg),
		ackrequeue.DefaultRequeueAfterDuration,
	)
}
