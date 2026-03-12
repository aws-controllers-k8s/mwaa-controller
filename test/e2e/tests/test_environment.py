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

"""Integration tests for the MWAA Environment resource.

MWAA environments are async (~25 min create, ~15 min update, ~10 min delete).
All tests are marked @pytest.mark.slow and require --runslow to execute.
"""

import datetime
import logging
import time

import boto3
import pytest

from acktest.k8s import resource as k8s
from acktest.k8s import condition
from acktest.resources import random_suffix_name
from e2e import service_marker, CRD_GROUP, CRD_VERSION, load_mwaa_resource
from e2e.replacement_values import REPLACEMENT_VALUES

RESOURCE_PLURAL = "environments"

# MWAA create takes ~25-35 min; poll every 60s for up to 50 min
CREATE_TIMEOUT_SECONDS = 60 * 50
# MWAA update takes ~15-35 min
UPDATE_TIMEOUT_SECONDS = 60 * 45
# MWAA delete takes ~10-25 min
DELETE_TIMEOUT_SECONDS = 60 * 30

POLL_INTERVAL_SECONDS = 60

# Time to let the controller reconcile after a k8s operation
RECONCILE_WAIT_SECONDS = 30


def wait_for_cr_status(ref, target_status, timeout_seconds):
    """Poll the CR until status.status matches target_status."""
    deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout_seconds)
    while datetime.datetime.now() < deadline:
        cr = k8s.get_resource(ref)
        status = cr.get("status", {}).get("status")
        logging.info(f"CR {ref.name} status: {status}")
        if status == target_status:
            return cr
        time.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(f"Timed out waiting for CR status to reach {target_status}")


def wait_for_environment_status(mwaa_client, env_name, target_status, timeout_seconds):
    """Poll MWAA API until environment reaches target_status or timeout."""
    deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout_seconds)
    while datetime.datetime.now() < deadline:
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
            raise
        time.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(f"Timed out waiting for environment {env_name} to reach {target_status}")


@service_marker
@pytest.mark.slow
class TestEnvironment:
    def test_create_update_delete(self, mwaa_client):
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

        # Wait for AVAILABLE in AWS
        wait_for_environment_status(mwaa_client, env_name, "AVAILABLE", CREATE_TIMEOUT_SECONDS)

        # Wait for controller to reconcile the AVAILABLE status onto the CR.
        # The controller requeues every 300s on success, so this may take a few minutes.
        cr = wait_for_cr_status(ref, "AVAILABLE", CREATE_TIMEOUT_SECONDS)

        # Verify Synced condition
        assert k8s.wait_on_condition(ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=10)

        # Verify in AWS
        aws_res = mwaa_client.get_environment(Name=env_name)
        assert aws_res["Environment"]["Status"] == "AVAILABLE"
        assert aws_res["Environment"]["EnvironmentClass"] == "mw1.micro"

        # Verify status fields populated on the CR
        assert "webserverURL" in cr["status"], "webserverURL should be set after AVAILABLE"
        assert ".airflow." in cr["status"]["webserverURL"]
        assert "createdAt" in cr["status"], "createdAt should be set after AVAILABLE"

        # --- Update: change WeeklyMaintenanceWindowStart ---
        # Use a lightweight field that doesn't require infrastructure changes.
        # Changing WebserverAccessMode or EnvironmentClass triggers heavy
        # reprovisioning that can take 45+ min.
        updates = {
            "spec": {
                "weeklyMaintenanceWindowStart": "SAT:03:00",
            },
        }
        k8s.patch_custom_resource(ref, updates)
        time.sleep(RECONCILE_WAIT_SECONDS)

        # Wait for update to complete
        wait_for_environment_status(mwaa_client, env_name, "AVAILABLE", UPDATE_TIMEOUT_SECONDS)
        wait_for_cr_status(ref, "AVAILABLE", UPDATE_TIMEOUT_SECONDS)

        # Verify Synced after update
        assert k8s.wait_on_condition(ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=10)

        # Verify update in AWS
        aws_res = mwaa_client.get_environment(Name=env_name)
        assert aws_res["Environment"]["WeeklyMaintenanceWindowStart"] == "SAT:03:00"

        # --- Delete ---
        _, deleted = k8s.delete_custom_resource(ref, 3, 10)
        assert deleted

        wait_for_environment_status(mwaa_client, env_name, "DELETED", DELETE_TIMEOUT_SECONDS)
