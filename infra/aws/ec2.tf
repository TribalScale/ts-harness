locals {
  rds_endpoint_rendered   = var.memory_backend == "rds" ? aws_db_instance.harness[0].address : ""
  rds_secret_arn_rendered = var.memory_backend == "rds" ? aws_db_instance.harness[0].master_user_secret[0].secret_arn : ""
  rds_db_name_rendered    = var.memory_backend == "rds" ? aws_db_instance.harness[0].db_name : "ts_harness_memory"
  rds_db_user_rendered    = var.memory_backend == "rds" ? aws_db_instance.harness[0].username : ""
  rds_db_port_rendered    = var.memory_backend == "rds" ? "5432" : "5432"

  # All __TF_TOKEN__ -> value mappings in one place.
  # To add a new Terraform variable: (1) add one entry to this map,
  # (2) add one _ud_sN staged-replacement local referencing it below,
  # (3) update the _ud_sN chain tail, and (4) add the __TF_TOKEN__ to user-data.sh.
  # For Terraform >= 1.8 the staged locals below can be replaced with:
  #   reduce(keys(local._ud_subs), file("..."), (acc, k) => replace(acc, k, local._ud_subs[k]))
  _ud_subs = {
    "__TF_ADMIN_USER__"                    = var.admin_username
    "__TF_AGENT_RUNTIME_VERSION__"         = var.agent_runtime_version
    "__TF_AWS_REGION__"                    = var.aws_region
    "__TF_CLOUD_PROVIDER__"                = "aws"
    "__TF_CLOUD_REGION__"                  = var.aws_region
    "__TF_SECRETS_PREFIX__"                = local.secret_prefix_effective
    "__TF_ENTERPRISE_REPO__"               = var.enterprise_repo_url
    "__TF_MEMORY_BACKEND__"                = var.memory_backend
    "__TF_RDS_ENDPOINT__"                  = local.rds_endpoint_rendered
    "__TF_RDS_SECRET_ARN__"                = local.rds_secret_arn_rendered
    "__TF_RDS_DB_NAME__"                   = local.rds_db_name_rendered
    "__TF_MANAGED_DB_HOST__"               = local.rds_endpoint_rendered
    "__TF_MANAGED_DB_PORT__"               = local.rds_db_port_rendered
    "__TF_MANAGED_DB_NAME__"               = local.rds_db_name_rendered
    "__TF_MANAGED_DB_USER__"               = local.rds_db_user_rendered
    "__TF_MANAGED_DB_CREDENTIAL_REF__"     = local.rds_secret_arn_rendered
    "__TF_CREDENTIALS_ENCRYPTION_KEY_ID__" = aws_kms_alias.agent_credentials.name
    # Instance id is not known when rendering launch-template user-data without a cycle; runtime IMDS fills it.
    "__TF_INSTANCE_RESOURCE_ID__" = ""
  }

  # Staged substitutions: each local applies the next token from _ud_subs.
  # The map above is the single source of truth for all variable values.
  _ud_s0  = file("${path.module}/../../scripts/user-data.sh")
  _ud_s1  = replace(local._ud_s0, "__TF_ADMIN_USER__", local._ud_subs["__TF_ADMIN_USER__"])
  _ud_s2  = replace(local._ud_s1, "__TF_AGENT_RUNTIME_VERSION__", local._ud_subs["__TF_AGENT_RUNTIME_VERSION__"])
  _ud_s3  = replace(local._ud_s2, "__TF_AWS_REGION__", local._ud_subs["__TF_AWS_REGION__"])
  _ud_s4  = replace(local._ud_s3, "__TF_CLOUD_PROVIDER__", local._ud_subs["__TF_CLOUD_PROVIDER__"])
  _ud_s5  = replace(local._ud_s4, "__TF_CLOUD_REGION__", local._ud_subs["__TF_CLOUD_REGION__"])
  _ud_s6  = replace(local._ud_s5, "__TF_SECRETS_PREFIX__", local._ud_subs["__TF_SECRETS_PREFIX__"])
  _ud_s7  = replace(local._ud_s6, "__TF_ENTERPRISE_REPO__", local._ud_subs["__TF_ENTERPRISE_REPO__"])
  _ud_s8  = replace(local._ud_s7, "__TF_MEMORY_BACKEND__", local._ud_subs["__TF_MEMORY_BACKEND__"])
  _ud_s9  = replace(local._ud_s8, "__TF_RDS_ENDPOINT__", local._ud_subs["__TF_RDS_ENDPOINT__"])
  _ud_s10 = replace(local._ud_s9, "__TF_RDS_SECRET_ARN__", local._ud_subs["__TF_RDS_SECRET_ARN__"])
  _ud_s11 = replace(local._ud_s10, "__TF_RDS_DB_NAME__", local._ud_subs["__TF_RDS_DB_NAME__"])
  _ud_s12 = replace(local._ud_s11, "__TF_MANAGED_DB_HOST__", local._ud_subs["__TF_MANAGED_DB_HOST__"])
  _ud_s13 = replace(local._ud_s12, "__TF_MANAGED_DB_PORT__", local._ud_subs["__TF_MANAGED_DB_PORT__"])
  _ud_s14 = replace(local._ud_s13, "__TF_MANAGED_DB_NAME__", local._ud_subs["__TF_MANAGED_DB_NAME__"])
  _ud_s15 = replace(local._ud_s14, "__TF_MANAGED_DB_USER__", local._ud_subs["__TF_MANAGED_DB_USER__"])
  _ud_s16 = replace(local._ud_s15, "__TF_MANAGED_DB_CREDENTIAL_REF__", local._ud_subs["__TF_MANAGED_DB_CREDENTIAL_REF__"])
  _ud_s17 = replace(local._ud_s16, "__TF_CREDENTIALS_ENCRYPTION_KEY_ID__", local._ud_subs["__TF_CREDENTIALS_ENCRYPTION_KEY_ID__"])
  _ud_s18 = replace(local._ud_s17, "__TF_INSTANCE_RESOURCE_ID__", local._ud_subs["__TF_INSTANCE_RESOURCE_ID__"])

  user_data_rendered = local._ud_s18
}

resource "aws_launch_template" "harness" {
  name_prefix            = "${var.project_name}-"
  image_id               = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  key_name               = var.ssh_key_name
  vpc_security_group_ids = [aws_security_group.harness.id]

  user_data = base64gzip(local.user_data_rendered)

  iam_instance_profile {
    name = aws_iam_instance_profile.harness.name
  }

  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_size           = var.root_volume_size
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
    # Containers in the memory-api sidecar need IMDSv2 role credentials when
    # embeddings_provider = "bedrock".
    http_put_response_hop_limit = 2
  }

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name                = var.project_name
      AgentRuntimeVersion = var.agent_runtime_version
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags = {
      Name = "${var.project_name}-root"
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_instance" "harness" {
  launch_template {
    id      = aws_launch_template.harness.id
    version = "$Latest"
  }

  tags = {
    Name                = var.project_name
    AgentRuntimeVersion = var.agent_runtime_version
  }

  lifecycle {
    ignore_changes = [launch_template]
  }
}
