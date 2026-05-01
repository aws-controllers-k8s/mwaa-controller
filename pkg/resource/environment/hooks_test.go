package environment

import (
	"errors"
	"strings"
	"testing"

	ackv1alpha1 "github.com/aws-controllers-k8s/runtime/apis/core/v1alpha1"
	ackcompare "github.com/aws-controllers-k8s/runtime/pkg/compare"
	ackrequeue "github.com/aws-controllers-k8s/runtime/pkg/requeue"
	svcsdktypes "github.com/aws/aws-sdk-go-v2/service/mwaa/types"
	"github.com/stretchr/testify/assert"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/aws-controllers-k8s/mwaa-controller/apis/v1alpha1"
)

func ptr[T any](v T) *T {
	return &v
}

// newEnvResource builds a minimal *resource wrapping a v1alpha1.Environment
// with the supplied LastUpdate set on its Status.
func newEnvResource(lu *v1alpha1.LastUpdate) *resource {
	return &resource{
		ko: &v1alpha1.Environment{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "test-env",
				Namespace: "test-ns",
			},
			Status: v1alpha1.EnvironmentStatus{
				LastUpdate: lu,
			},
		},
	}
}

// syncedCondition returns the ACK.ResourceSynced Condition on r, or nil.
func syncedCondition(r *resource) *ackv1alpha1.Condition {
	for _, c := range r.ko.Status.Conditions {
		if c.Type == ackv1alpha1.ConditionTypeResourceSynced {
			return c
		}
	}
	return nil
}

func TestHandleUpdateFailed(t *testing.T) {
	failedStatus := string(svcsdktypes.UpdateStatusFailed)
	successStatus := string(svcsdktypes.UpdateStatusSuccess)

	tests := []struct {
		name          string
		lastUpdate    *v1alpha1.LastUpdate
		wantResource  bool // true => expect non-nil returned resource
		wantRequeue   bool // true => expect *ackrequeue.RequeueNeededAfter
		wantCondition bool // true => expect Synced=False condition set
		wantMsgParts  []string
	}{
		{
			name:          "nil LastUpdate is a no-op",
			lastUpdate:    nil,
			wantResource:  false,
			wantRequeue:   false,
			wantCondition: false,
		},
		{
			name: "nil LastUpdate.Status is a no-op",
			lastUpdate: &v1alpha1.LastUpdate{
				Status: nil,
			},
			wantResource:  false,
			wantRequeue:   false,
			wantCondition: false,
		},
		{
			name: "SUCCESS is a no-op",
			lastUpdate: &v1alpha1.LastUpdate{
				Status: &successStatus,
			},
			wantResource:  false,
			wantRequeue:   false,
			wantCondition: false,
		},
		{
			name: "FAILED with full error code and message",
			lastUpdate: &v1alpha1.LastUpdate{
				Status: &failedStatus,
				Error: &v1alpha1.UpdateError{
					ErrorCode:    ptr("InvalidRequest"),
					ErrorMessage: ptr("bad network config"),
				},
			},
			wantResource:  true,
			wantRequeue:   true,
			wantCondition: true,
			wantMsgParts: []string{
				"last UpdateEnvironment failed",
				"InvalidRequest",
				"bad network config",
				"patch the spec to retry",
			},
		},
		{
			name: "FAILED with nil Error uses fallbacks",
			lastUpdate: &v1alpha1.LastUpdate{
				Status: &failedStatus,
				Error:  nil,
			},
			wantResource:  true,
			wantRequeue:   true,
			wantCondition: true,
			wantMsgParts: []string{
				"last UpdateEnvironment failed",
				"Unknown",
				"update failed with no error details",
				"patch the spec to retry",
			},
		},
		{
			name: "FAILED with only ErrorCode falls back on message",
			lastUpdate: &v1alpha1.LastUpdate{
				Status: &failedStatus,
				Error: &v1alpha1.UpdateError{
					ErrorCode: ptr("AccessDenied"),
				},
			},
			wantResource:  true,
			wantRequeue:   true,
			wantCondition: true,
			wantMsgParts: []string{
				"last UpdateEnvironment failed",
				"AccessDenied",
				"update failed with no error details",
				"patch the spec to retry",
			},
		},
		{
			name: "FAILED with only ErrorMessage falls back on code",
			lastUpdate: &v1alpha1.LastUpdate{
				Status: &failedStatus,
				Error: &v1alpha1.UpdateError{
					ErrorMessage: ptr("something broke"),
				},
			},
			wantResource:  true,
			wantRequeue:   true,
			wantCondition: true,
			wantMsgParts: []string{
				"last UpdateEnvironment failed",
				"Unknown",
				"something broke",
				"patch the spec to retry",
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			r := newEnvResource(tt.lastUpdate)

			gotRes, gotErr := handleUpdateFailed(r)

			if !tt.wantResource {
				assert.Nil(t, gotRes, "expected nil resource for no-op case")
				assert.NoError(t, gotErr, "expected nil error for no-op case")
				assert.Nil(t, syncedCondition(r), "expected no Synced condition for no-op case")
				return
			}

			// Failed-update path: returned resource should be the input
			// resource itself (wrapping the same ko).
			assert.NotNil(t, gotRes, "expected non-nil resource for failed case")
			assert.Same(t, r, gotRes, "expected helper to return the same *resource")

			// Error should be a non-terminal requeue error.
			if tt.wantRequeue {
				var rq *ackrequeue.RequeueNeededAfter
				assert.True(t, errors.As(gotErr, &rq),
					"expected error to be *ackrequeue.RequeueNeededAfter, got %T: %v", gotErr, gotErr)
				if rq != nil {
					assert.Equal(t, ackrequeue.DefaultRequeueAfterDuration, rq.Duration(),
						"expected DefaultRequeueAfterDuration")
				}
			}

			// ACK.ResourceSynced condition should be set to False with a
			// message containing the code + message fragments.
			if tt.wantCondition {
				c := syncedCondition(r)
				assert.NotNil(t, c, "expected ACK.ResourceSynced condition to be set")
				if c != nil {
					assert.Equal(t, corev1.ConditionFalse, c.Status,
						"expected ACK.ResourceSynced=False")
					assert.NotNil(t, c.Message, "expected condition message to be set")
					if c.Message != nil {
						for _, frag := range tt.wantMsgParts {
							assert.True(t, strings.Contains(*c.Message, frag),
								"expected condition message %q to contain %q", *c.Message, frag)
						}
					}
				}
			}
		})
	}
}

// newEnvWithAirflowConfig builds a *resource whose Spec.AirflowConfigurationOptions
// is set to the supplied map. Used to test customPreCompare.
func newEnvWithAirflowConfig(opts map[string]*string) *resource {
	return &resource{
		ko: &v1alpha1.Environment{
			Spec: v1alpha1.EnvironmentSpec{
				AirflowConfigurationOptions: opts,
			},
		},
	}
}

// TestCustomPreCompare_RedactsAirflowConfigurationOptions is the most important
// assertion in this file: it locks in that customPreCompare passes nil
// placeholders (not the raw map values) to delta.Add. This is what prevents
// the runtime reconciler's `rlog.Info("diff", delta.Differences)` from
// leaking user secrets stored in Spec.AirflowConfigurationOptions into the
// controller logs. A future regeneration must never silently reintroduce the
// raw values.
func TestCustomPreCompare_RedactsAirflowConfigurationOptions(t *testing.T) {
	tests := []struct {
		name         string
		a            map[string]*string
		b            map[string]*string
		wantDiff     bool
		wantDiffPath string
	}{
		{
			name:     "both nil => no diff",
			a:        nil,
			b:        nil,
			wantDiff: false,
		},
		{
			name:     "both empty => no diff",
			a:        map[string]*string{},
			b:        map[string]*string{},
			wantDiff: false,
		},
		{
			name:     "nil vs empty => no diff (len == len)",
			a:        nil,
			b:        map[string]*string{},
			wantDiff: false,
		},
		{
			name:     "equal maps => no diff",
			a:        map[string]*string{"core.fernet_key": ptr("SECRET_A"), "core.dag_concurrency": ptr("16")},
			b:        map[string]*string{"core.fernet_key": ptr("SECRET_A"), "core.dag_concurrency": ptr("16")},
			wantDiff: false,
		},
		{
			name:         "different length => diff at Spec.AirflowConfigurationOptions",
			a:            map[string]*string{"core.fernet_key": ptr("SECRET_A")},
			b:            map[string]*string{"core.fernet_key": ptr("SECRET_A"), "core.dag_concurrency": ptr("16")},
			wantDiff:     true,
			wantDiffPath: "Spec.AirflowConfigurationOptions",
		},
		{
			name:         "same length, different values => diff at Spec.AirflowConfigurationOptions",
			a:            map[string]*string{"core.fernet_key": ptr("SECRET_OLD")},
			b:            map[string]*string{"core.fernet_key": ptr("SECRET_NEW")},
			wantDiff:     true,
			wantDiffPath: "Spec.AirflowConfigurationOptions",
		},
		{
			name:         "same length, different keys => diff at Spec.AirflowConfigurationOptions",
			a:            map[string]*string{"core.fernet_key": ptr("x")},
			b:            map[string]*string{"core.dag_concurrency": ptr("x")},
			wantDiff:     true,
			wantDiffPath: "Spec.AirflowConfigurationOptions",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			a := newEnvWithAirflowConfig(tt.a)
			b := newEnvWithAirflowConfig(tt.b)
			delta := ackcompare.NewDelta()

			customPreCompare(delta, a, b)

			if !tt.wantDiff {
				assert.Empty(t, delta.Differences,
					"expected customPreCompare to add no differences, got %d", len(delta.Differences))
				return
			}

			// Exactly one diff should be added for AirflowConfigurationOptions.
			if !assert.Len(t, delta.Differences, 1, "expected exactly one difference") {
				return
			}
			d := delta.Differences[0]

			// The diff must identify the path so reconciler routing
			// (Delta.DifferentAt / DifferentExcept) still works.
			assert.True(t, d.Path.Contains(tt.wantDiffPath),
				"expected diff path to contain %q", tt.wantDiffPath)
			assert.True(t, delta.DifferentAt(tt.wantDiffPath),
				"expected Delta.DifferentAt(%q) == true", tt.wantDiffPath)

			// The redaction guarantee: A and B must be nil so the runtime
			// reconciler cannot log the underlying map values.
			assert.Nil(t, d.A, "expected Difference.A to be nil (redacted), got %#v", d.A)
			assert.Nil(t, d.B, "expected Difference.B to be nil (redacted), got %#v", d.B)
		})
	}
}

// TestCustomPreCompare_PreservesControlFlow verifies that even with scrubbed
// A/B values, the path-based helpers Delta.DifferentAt and Delta.DifferentExcept
// continue to function. These helpers are what reconciler.go uses to decide
// whether to call Update, so this is the behavioral guarantee that the
// redaction does not change control flow.
func TestCustomPreCompare_PreservesControlFlow(t *testing.T) {
	a := newEnvWithAirflowConfig(map[string]*string{"core.fernet_key": ptr("old")})
	b := newEnvWithAirflowConfig(map[string]*string{"core.fernet_key": ptr("new")})
	delta := ackcompare.NewDelta()

	customPreCompare(delta, a, b)

	assert.True(t, delta.DifferentAt("Spec.AirflowConfigurationOptions"),
		"expected reconciler to observe a diff at Spec.AirflowConfigurationOptions")
	assert.True(t, delta.DifferentAt("Spec"),
		"expected Delta.DifferentAt(\"Spec\") == true (reconciler's update trigger)")
	// DifferentExcept("Spec.AirflowConfigurationOptions") should be false:
	// the only diff is the scrubbed one, nothing else.
	assert.False(t, delta.DifferentExcept("Spec.AirflowConfigurationOptions"),
		"expected no diffs other than Spec.AirflowConfigurationOptions")
}
