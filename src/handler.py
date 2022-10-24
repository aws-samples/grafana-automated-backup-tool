#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
""" Lambda function to schedule grafana backups and upload them to s3 bucket
    The function can be also used to perform restore operations."""

import os

import logging
import boto3
import json

from grafana_backup.save import main as save
from grafana_backup.restore import main as restore
from grafana_backup.grafanaSettings import main as conf


# Global variables
logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger()
BACKUP, RESTORE = "backup", "restore"

# initiate Boto3 clients
s3 = boto3.client("s3")
ssm = boto3.client("ssm")
sts = boto3.client("sts")

def _get_parameters():
    """Get and validate paramaters"""
    parameters = {}
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    grafana_url = os.environ.get("GRAFANA_URL")
    bucket_name = os.environ.get("BUCKET_NAME")
    bucket_prefix = os.environ.get("BUCKET_PREFIX")
    secret_name = os.environ.get("API_TOKEN_PARAMETER")
    if log_level == "INFO":
        LOG.setLevel(logging.INFO)
    elif log_level == "WARNING":
        LOG.setLevel(logging.WARNING)
    elif log_level == "ERROR":
        LOG.setLevel(logging.ERROR)
    LOG.info(f"Using Grafana: {grafana_url}")
    parameters["grafana_url"] = grafana_url
    LOG.info(f"Using Bucket: {bucket_name}")
    parameters["bucket_name"] = bucket_name
    LOG.info(f"Using Bucket Prefix: {bucket_prefix}")
    parameters["bucket_prefix"] = bucket_prefix
    LOG.info(f"Using Buckup dir: /tmp/{bucket_prefix}")
    parameters["backup_dir"] = f"/tmp/{bucket_prefix}"
    LOG.info(f"Using SSM Secure parameter: {secret_name}")
    parameters["secret_name"] = secret_name
    return parameters


def _configure_grafana(parameters):
    """Set grafana configuration."""
    grafana_url = parameters.get("grafana_url")
    secret_name = parameters.get("secret_name")
    backup_dir = parameters.get("backup_dir")
    token = _get_grafana_token(secret_name)
    LOG.info("Configuring Grafana")
    grafana_settings = {
        "general": {
            "debug": False,
            "verify_ssl": True,
            "api_health_check": False,
            "backup_dir": backup_dir,
            "pretty_print": False,
        },
        "grafana": {
            "url": grafana_url,
            "token": token,
            "search_api_limit": 5000,
            "default_password": "",
            "admin_account": "",
            "admin_password": "",
        },
    }
    grafana_config = "/tmp/grafanaSettings.json"
    with open(grafana_config, "w") as settingsfile:
        settingsfile.write(json.dumps(grafana_settings))
    settings = conf(grafana_config)
    return settings


def _get_grafana_token(secret_name):
    """Get grafana token from SSM Secure parameter."""
    LOG.info("Getting Grafana Token")
    response = ssm.get_parameter(Name=secret_name, WithDecryption=True)
    token = response.get("Parameter").get("Value")
    return token


def _get_latest_backup(parameters):
    """Get latest backup to restore."""
    LOG.info("Retrieving latest available Backup")
    bucket_name = parameters.get("bucket_name")
    objs = s3.list_objects_v2(Bucket=bucket_name)["Contents"]
    last_modified = max(objs, key=lambda x: x["LastModified"])
    latest_backup = os.path.basename(last_modified["Key"])
    LOG.info(f"latest available Backup: {latest_backup}")
    return latest_backup


def _download_backup(parameters, backup_file):
    """Download backup file."""
    LOG.info(f"Downloading Backup file: {backup_file}")
    bucket_name = parameters.get("bucket_name")
    bucket_prefix = parameters.get("bucket_prefix")
    backup_dir = parameters.get("backup_dir")
    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)
    dst_file = f"{backup_dir}/{backup_file}"
    src_file = f"{bucket_prefix}/{backup_file}"
    s3.download_file(bucket_name, src_file, dst_file)
    return dst_file


def _upload_backup(parameters):
    """Upload backup file."""
    bucket_name = parameters.get("bucket_name")
    bucket_prefix = parameters.get("bucket_prefix")
    backup_dir = parameters.get("backup_dir")
    backup_file = os.listdir(backup_dir)[0]
    account_id = sts.get_caller_identity().get('Account')
    LOG.info(f"Uploading Backup file: {backup_file}")
    s3.upload_file(
        f"{backup_dir}/{backup_file}",
        bucket_name,
        f"{bucket_prefix}/{backup_file}",
        ExtraArgs={'ExpectedBucketOwner': account_id}
    )


def handler(event, context):
    """Lambda handler."""

    # Initiate parameters
    operation = event.get("operation") or BACKUP

    # Configure garafana API connection
    parameters = _get_parameters()
    settings = _configure_grafana(parameters)

    if BACKUP == operation:
        # Backyp Grafana
        components = (
            event.get("components") or "folders,dashboards,datasources,alert-channels"
        )
        args = {
            "--components": components,
            "--config": None,
        }
        save(args, settings)
        _upload_backup(parameters)
    elif RESTORE == operation:
        # Restore Grafana
        components = (
            event.get("components") or "folders,dashboards,datasources,alert-channels"
        )
        backup_filename = event.get("backup_file") or _get_latest_backup(parameters)
        backup_file = _download_backup(parameters, backup_filename)
        args = {
            "<archive_file>": backup_file,
            "--components": components,
        }
        restore(args, settings)

    # Exit lambda function
    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": f"{operation} finished successfully",
            }
        ),
    }
