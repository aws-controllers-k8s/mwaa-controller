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
"""Bootstraps the resources required to run the MWAA integration tests.

Creates:
- IAM execution role (trusted by airflow.amazonaws.com and airflow-env.amazonaws.com)
- S3 bucket for DAGs (with dags/ prefix)
- VPC with 2 public + 2 private subnets and a self-referencing ingress rule
  on the VPC's default security group

All helpers used here are generic bootstrappers in `acktest.bootstrapping`;
MWAA-specific behaviour is expressed through their optional parameters
(multi-principal trust, bucket prefix pre-creation, self-referencing SG
ingress).
"""

import json
import logging

from acktest.bootstrapping import Resources, BootstrapFailureException
from acktest.bootstrapping.iam import Role, UserPolicies
from acktest.bootstrapping.s3 import Bucket
from acktest.bootstrapping.vpc import VPC
from e2e import bootstrap_directory
from e2e.bootstrap_resources import BootstrapResources


def service_bootstrap() -> Resources:
    logging.getLogger().setLevel(logging.INFO)

    execution_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject*",
                    "s3:GetBucket*",
                    "s3:List*",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogStream",
                    "logs:CreateLogGroup",
                    "logs:PutLogEvents",
                    "logs:GetLogEvents",
                    "logs:GetLogRecord",
                    "logs:GetLogGroupFields",
                    "logs:GetQueryResults",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "sqs:ChangeMessageVisibility",
                    "sqs:DeleteMessage",
                    "sqs:GetQueueAttributes",
                    "sqs:GetQueueUrl",
                    "sqs:ReceiveMessage",
                    "sqs:SendMessage",
                ],
                "Resource": "arn:aws:sqs:*:*:airflow-celery-*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:GenerateDataKey*",
                    "kms:Encrypt",
                ],
                "Resource": "*",
                "Condition": {
                    "StringLike": {
                        "kms:ViaService": [
                            "sqs.*.amazonaws.com",
                            "s3.*.amazonaws.com",
                        ]
                    }
                },
            },
            {
                "Effect": "Allow",
                "Action": "airflow:PublishMetrics",
                "Resource": "*",
            },
        ],
    })

    # MWAA needs both airflow.amazonaws.com (control plane) and
    # airflow-env.amazonaws.com (data plane running on customer subnets) to
    # assume the execution role; the trust policy must include both.
    resources = BootstrapResources(
        ExecutionRole=Role(
            name_prefix="ack-mwaa-execution-role",
            principal_service="airflow.amazonaws.com",
            additional_service_principals=["airflow-env.amazonaws.com"],
            user_policies=UserPolicies(
                "ack-mwaa-execution-policy", [execution_policy],
            ),
        ),
        # MWAA `CreateEnvironment` validates that the DAGs prefix exists on
        # the source bucket, so pre-create `dags/` as a zero-byte object.
        DAGBucket=Bucket(
            name_prefix="ack-mwaa-dags",
            enable_versioning=True,
            empty_objects=["dags/"],
        ),
        # MWAA requires the environment's security group to allow
        # self-referencing inbound traffic between workers, schedulers, and
        # the webserver.
        EnvironmentVPC=VPC(
            name_prefix="mwaa-vpc",
            num_public_subnet=2,
            num_private_subnet=2,
            security_group_self_referencing_ingress=True,
        ),
    )

    try:
        resources.bootstrap()
    except BootstrapFailureException:
        exit(254)

    return resources


if __name__ == "__main__":
    config = service_bootstrap()
    config.serialize(bootstrap_directory)
