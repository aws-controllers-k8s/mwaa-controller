# Copyright Amazon.com Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may
# not use this file except in compliance with the License. A copy of the
# License is located at
#
# 	 http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

"""Integration test for the MWAA Environment resource.

MWAA environments are async (~25-35 min create, ~15-35 min update,
~10-25 min delete), so a full create -> update -> delete cycle fits in
well under two hours.

Everything runs as a single test function (`test_environment_lifecycle`)
rather than split across multiple methods. The shared ACK test-infra
runner invokes pytest with `-n auto`, which otherwise causes
pytest-xdist to split multiple test methods across workers; each worker
would then provision its own MWAA environment in parallel. A single
test item is pinned to a single worker under xdist's LoadScheduling,
regardless of worker count, so this is the simplest way to guarantee
one Environment is in flight at a time.

Marked @pytest.mark.canary so mwaa-kind-e2e runs it.
"""

import datetime
import logging
import time

import pytest

from acktest.k8s import resource as k8s
from acktest.k8s import condition
from acktest.resources import random_suffix_name
from e2e import service_marker, CRD_GROUP, CRD_VERSION, load_mwaa_resource
from e2e.replacement_values import REPLACEMENT_VALUES

RESOURCE_PLURAL = "environments"

# MWAA create takes ~25-35 min typically but can exceed 90 min in a busy
# test account; cap at 120 min to absorb that. Poll every 60s.
CREATE_TIMEOUT_SECONDS = 60 * 120
# MWAA update takes ~15-35 min typically but we have observed >45 min in
# the test account, so give it 120 min of headroom.
UPDATE_TIMEOUT_SECONDS = 60 * 120
# MWAA delete takes ~10-25 min typically; give it 60 min of headroom.
DELETE_TIMEOUT_SECONDS = 60 * 60

POLL_INTERVAL_SECONDS = 60

# Time to let the controller reconcile after a k8s operation
RECONCILE_WAIT_SECONDS = 30


def wait_for_cr_status(ref, target_status, timeout_seconds):
    """Poll the CR until status.status matches target_status."""
    deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout_seconds)
    while datetime.datetime.now() < deadline:
        try:
            cr = k8s.get_resource(ref)
            status = cr.get("status", {}).get("status")
            logging.info(f"CR {ref.name} status: {status}")
            if status == target_status:
                return cr
        except Exception as e:
            logging.warning(f"Transient error getting CR {ref.name}: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(f"Timed out waiting for CR status to reach {target_status}")


def wait_for_environment_status(mwaa_client, env_name, target_status, timeout_seconds, cr_ref=None):
    """Poll MWAA API until environment reaches target_status or timeout.

    If ``cr_ref`` is provided, also inspect the CR's conditions on each
    iteration and fast-fail if the controller has set ACK.Terminal=True.
    Without this, a terminal error surfaced only on the CR (e.g. a
    ValidationException that prevents the CreateEnvironment call from ever
    reaching AWS) would be invisible to this poll loop because
    ``get_environment`` keeps returning ResourceNotFoundException until
    timeout. Pass ``cr_ref=None`` when polling for DELETED because the CR is
    intentionally being torn down.
    """
    deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout_seconds)
    while datetime.datetime.now() < deadline:
        # Fail fast if the controller marked the CR terminal.
        if cr_ref is not None:
            try:
                cr = k8s.get_resource(cr_ref)
                for cond in (cr or {}).get("status", {}).get("conditions", []) or []:
                    if (cond.get("type") == condition.CONDITION_TYPE_TERMINAL
                            and cond.get("status") == "True"):
                        pytest.fail(
                            f"Controller set {condition.CONDITION_TYPE_TERMINAL} on "
                            f"{cr_ref.name}: {cond.get('message')}"
                        )
            except Exception as e:
                # CR read failure is transient; keep polling.
                logging.warning(f"Transient error reading CR {cr_ref.name}: {e}")

        try:
            resp = mwaa_client.get_environment(Name=env_name)
            status = resp["Environment"]["Status"]
            logging.info(f"Environment {env_name}: {status}")
            if status == target_status:
                return status
            if status in ("CREATE_FAILED", "UNAVAILABLE"):
                pytest.fail(f"Environment entered terminal failure state: {status}")
        except mwaa_client.exceptions.ResourceNotFoundException:
            if target_status == "DELETED":
                return "DELETED"
            # Controller may not have issued CreateEnvironment yet, or the
            # MWAA API is eventually consistent. Keep polling until timeout.
            logging.info(f"Environment {env_name} not yet visible in AWS; continuing to poll")
        time.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(f"Timed out waiting for environment {env_name} to reach {target_status}")


@service_marker
@pytest.mark.canary
def test_environment_lifecycle(mwaa_client):
    """End-to-end create -> update -> delete for an MWAA Environment.

    Uses WeeklyMaintenanceWindowStart for the update step because changing
    WebserverAccessMode or EnvironmentClass triggers heavy reprovisioning
    that can take 45+ minutes.
    """
    env_name = random_suffix_name("ack-mwaa", 32)

    replacements = REPLACEMENT_VALUES.copy()
    replacements["ENVIRONMENT_NAME"] = env_name

    resource_data = load_mwaa_resource(
        "environment",
        additional_replacements=replacements,
    )
    logging.debug(resource_data)

    ref = k8s.CustomResourceReference(
        CRD_GROUP, CRD_VERSION, RESOURCE_PLURAL,
        env_name, namespace="default",
    )

    # --- Create ---
    k8s.create_custom_resource(ref, resource_data)
    cr = k8s.wait_resource_consumed_by_controller(ref)
    assert cr is not None
    assert k8s.get_resource_exists(ref)

    try:
        # Wait for AVAILABLE in AWS first, then on the CR.
        wait_for_environment_status(
            mwaa_client, env_name, "AVAILABLE", CREATE_TIMEOUT_SECONDS,
            cr_ref=ref,
        )
        cr = wait_for_cr_status(ref, "AVAILABLE", CREATE_TIMEOUT_SECONDS)

        # Verify Synced condition
        assert k8s.wait_on_condition(
            ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=10,
        )

        # Verify in AWS
        aws_res = mwaa_client.get_environment(Name=env_name)
        assert aws_res["Environment"]["Status"] == "AVAILABLE"
        assert aws_res["Environment"]["EnvironmentClass"] == "mw1.micro"

        # Verify status fields populated on the CR
        assert "webserverURL" in cr["status"], \
            "webserverURL should be set after AVAILABLE"
        assert ".airflow." in cr["status"]["webserverURL"]
        assert "createdAt" in cr["status"], \
            "createdAt should be set after AVAILABLE"

        # --- Update ---
        updates = {
            "spec": {
                "weeklyMaintenanceWindowStart": "SAT:03:00",
            },
        }
        k8s.patch_custom_resource(ref, updates)
        time.sleep(RECONCILE_WAIT_SECONDS)

        # Wait for update to complete
        wait_for_environment_status(
            mwaa_client, env_name, "AVAILABLE", UPDATE_TIMEOUT_SECONDS,
            cr_ref=ref,
        )
        wait_for_cr_status(ref, "AVAILABLE", UPDATE_TIMEOUT_SECONDS)

        # Verify Synced after update
        assert k8s.wait_on_condition(
            ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=10,
        )

        # Verify update in AWS
        aws_res = mwaa_client.get_environment(Name=env_name)
        assert aws_res["Environment"]["WeeklyMaintenanceWindowStart"] == "SAT:03:00"

    finally:
        # --- Delete (teardown) ---
        # Best-effort: don't let a teardown error mask a test-body failure,
        # but still log it and wait for the environment to actually disappear
        # so we don't leak AWS resources.
        try:
            _, deleted = k8s.delete_custom_resource(ref, 3, 10)
            if not deleted:
                logging.warning(
                    f"delete_custom_resource returned False for {env_name}"
                )
            wait_for_environment_status(
                mwaa_client, env_name, "DELETED", DELETE_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logging.error(f"Teardown failed for {env_name}: {e}")
