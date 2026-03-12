# Copyright Amazon.com Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may
# not use this file except in compliance with the License. A copy of the
# License is located at
#
#	 http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.
"""Stores the values used by each of the integration tests for replacing the
MWAA-specific test variables.
"""

from e2e.bootstrap_resources import get_bootstrap_resources

REPLACEMENT_VALUES = {
    "EXECUTION_ROLE_ARN": get_bootstrap_resources().ExecutionRole.arn,
    "SOURCE_BUCKET_ARN": f"arn:aws:s3:::{get_bootstrap_resources().DAGBucket.name}",
    "PRIVATE_SUBNET_1": get_bootstrap_resources().EnvironmentVPC.private_subnets.subnet_ids[0],
    "PRIVATE_SUBNET_2": get_bootstrap_resources().EnvironmentVPC.private_subnets.subnet_ids[1],
    "SECURITY_GROUP": get_bootstrap_resources().EnvironmentVPC.security_group.group_id,
}
