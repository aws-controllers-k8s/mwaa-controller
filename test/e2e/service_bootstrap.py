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
- VPC with 2 private subnets (required by MWAA)
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

    resources = BootstrapResources(
        ExecutionRole=Role(
            "ack-mwaa-execution-role",
            "airflow.amazonaws.com",
            user_policies=UserPolicies("ack-mwaa-execution-policy", [execution_policy]),
        ),
        DAGBucket=Bucket("ack-mwaa-dags", enable_versioning=True),
        EnvironmentVPC=VPC(name_prefix="mwaa-vpc", num_public_subnet=2, num_private_subnet=2),
    )

    try:
        resources.bootstrap()
    except BootstrapFailureException:
        exit(254)

    import boto3

    # acktest Role only accepts a single service principal, but MWAA requires
    # both airflow.amazonaws.com and airflow-env.amazonaws.com.
    iam = boto3.client("iam")
    iam.update_assume_role_policy(
        RoleName=resources.ExecutionRole.name,
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": ["airflow.amazonaws.com", "airflow-env.amazonaws.com"]},
                "Action": "sts:AssumeRole"
            }]
        })
    )

    # MWAA requires the DAGs S3 path to exist at create time.
    s3 = boto3.client("s3")
    s3.put_object(Bucket=resources.DAGBucket.name, Key="dags/", Body=b"")

    # MWAA requires the security group to allow self-referencing inbound traffic.
    ec2 = boto3.client("ec2")
    sg_id = resources.EnvironmentVPC.security_group.group_id
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{
                "IpProtocol": "-1",
                "UserIdGroupPairs": [{"GroupId": sg_id}],
            }],
        )
    except ec2.exceptions.ClientError as e:
        if "Duplicate" not in str(e):
            raise

    return resources


if __name__ == "__main__":
    config = service_bootstrap()
    config.serialize(bootstrap_directory)
