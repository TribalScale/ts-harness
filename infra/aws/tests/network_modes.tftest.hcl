# Offline plan checks for network_mode and deployment_profile.
# Run: terraform init -backend=false && terraform test

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["us-east-1a", "us-east-1b", "us-east-1c"]
    }
  }

  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  mock_data "aws_subnets" {
    defaults = {
      ids = ["subnet-default-a", "subnet-default-b"]
    }
  }
}

mock_provider "random" {}

variables {
  ssh_key_name      = "test"
  ssh_allowed_cidrs = ["203.0.113.10/32"]
}

run "single_host_default_network_adds_nothing" {
  command = plan

  assert {
    condition     = length(module.vpc) == 0
    error_message = "default network mode must not build a VPC"
  }

  assert {
    condition     = length(aws_s3_bucket.skills) == 0 && length(aws_s3_bucket.workspaces) == 0 && length(aws_iam_role_policy.object_storage) == 0
    error_message = "single_host keeps skills and workspaces on the host"
  }

  assert {
    condition     = local.runtime_config_object.TS_HARNESS_STATE_BACKEND == "local" && local.runtime_config_object.TS_HARNESS_OBJECT_BACKEND == "local" && local.runtime_config_object.TS_HARNESS_SKILLS_BUCKET == ""
    error_message = "single_host defaults both storage backends to local"
  }

  assert {
    condition     = length(aws_ecs_cluster.coding) == 0 && length(aws_lb.memory_api) == 0
    error_message = "single_host must not create ECS or the internal load balancer"
  }

  assert {
    condition     = local.vpc_id == null && local.host_subnet_id == null
    error_message = "default network mode keeps the host security group in the default VPC"
  }

  assert {
    condition     = local.runtime_config_object.CODING_ECS_CLUSTER == ""
    error_message = "single_host publishes no ECS cluster"
  }
}

run "scale_out_requires_vpc" {
  command = plan

  variables {
    deployment_profile = "scale_out"
    memory_backend     = "rds"
  }

  expect_failures = [
    terraform_data.scale_out_network,
  ]
}

run "scale_out_in_vpc" {
  command = plan

  variables {
    deployment_profile = "scale_out"
    network_mode       = "vpc"
    memory_backend     = "rds"
  }

  assert {
    condition     = length(module.vpc) == 1
    error_message = "vpc mode builds the VPC module"
  }

  assert {
    condition     = aws_lb.memory_api[0].internal
    error_message = "memory API load balancer must be internal"
  }

  assert {
    condition     = aws_lb_listener.memory_api[0].default_action[0].type == "fixed-response"
    error_message = "unlisted memory API paths must not be forwarded"
  }

  assert {
    condition     = flatten([for c in aws_lb_listener_rule.memory_api_worker_routes[0].condition : [for p in c.path_pattern : sort(p.values)]]) == ["/coding-jobs/*", "/health"]
    error_message = "only health and coding-job routes reach the memory API"
  }

  assert {
    condition     = aws_ecs_task_definition.coding_worker[0].ephemeral_storage[0].size_in_gib == 50
    error_message = "workers get 50 GiB of ephemeral storage by default"
  }

  assert {
    condition     = aws_ecs_task_definition.coding_worker[0].cpu == "2048" && aws_ecs_task_definition.coding_worker[0].memory == "4096"
    error_message = "workers default to 2 vCPU and 4 GB"
  }

  assert {
    condition     = !aws_ecs_service.egress_proxy[0].network_configuration[0].assign_public_ip
    error_message = "the egress proxy runs without a public IP"
  }

  assert {
    condition     = local.runtime_config_object.CODING_ECS_TASK_DEFINITION == "ts-harness-coding-worker"
    error_message = "runtime config names the worker task family"
  }

  assert {
    condition     = contains(keys(local.all_secrets), "coding-job-secret")
    error_message = "scale_out needs a dedicated coding job signing secret"
  }

  assert {
    condition     = length(aws_vpc_endpoint.interface) == 5 && length(aws_vpc_endpoint.s3) == 1
    error_message = "workers reach ECR, Secrets Manager, Logs and SSM through VPC endpoints"
  }

  assert {
    condition     = length(aws_lb_listener_rule.llm_proxy_route) == 0 && contains(keys(local.coding_worker_secret_env), "CODING_BACKEND_API_KEY")
    error_message = "without the LLM proxy, workers keep the provider key and /llm/* is not routed"
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.coding_job_queue_age.threshold == 900 && aws_cloudwatch_metric_alarm.coding_job_queue_age.metric_name == "CodingJobOldestQueuedSeconds"
    error_message = "the oldest queued coding job alarms after 15 minutes"
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.github_rate_limit_low.threshold == 10 && aws_cloudwatch_metric_alarm.coding_job_lost_rate.threshold == 0.2
    error_message = "GitHub quota alarms below 10% and the lost-job rate alarm defaults to 20%"
  }

  assert {
    condition     = length(jsondecode(aws_cloudwatch_dashboard.scale_out.dashboard_body).widgets) == 13
    error_message = "the scale-out dashboard covers lanes, coding jobs, gates, GitHub and LLM spend"
  }

  assert {
    condition     = aws_s3_bucket.skills[0].bucket == "ts-harness-skills" && aws_s3_bucket.workspaces[0].bucket == "ts-harness-workspaces"
    error_message = "scale_out creates the skills and workspaces buckets"
  }

  assert {
    condition     = local.runtime_config_object.TS_HARNESS_STATE_BACKEND == "postgres" && local.runtime_config_object.TS_HARNESS_OBJECT_BACKEND == "s3"
    error_message = "scale_out defaults to postgres state and s3 objects"
  }

  assert {
    condition     = alltrue([for b in aws_s3_bucket_public_access_block.objects : b.block_public_acls && b.block_public_policy && b.restrict_public_buckets]) && length(aws_s3_bucket_public_access_block.objects) == 2
    error_message = "both buckets block public access"
  }

  assert {
    condition     = alltrue([for v in aws_s3_bucket_versioning.objects : v.versioning_configuration[0].status == "Enabled"]) && length(aws_s3_bucket_versioning.objects) == 2
    error_message = "both buckets keep object versions"
  }

  assert {
    condition     = alltrue([for e in aws_s3_bucket_server_side_encryption_configuration.objects : [for r in e.rule : [for d in r.apply_server_side_encryption_by_default : d.sse_algorithm]][0][0] == "aws:kms"])
    error_message = "both buckets encrypt with KMS"
  }

  assert {
    condition     = [for r in aws_s3_bucket_lifecycle_configuration.skills[0].rule : r.id] == ["expire-noncurrent-versions", "expire-abandoned-drafts"] && aws_s3_bucket_lifecycle_configuration.skills[0].rule[1].expiration[0].days == 30 && aws_s3_bucket_lifecycle_configuration.skills[0].rule[0].noncurrent_version_expiration[0].noncurrent_days == 90
    error_message = "skills keep old versions 90 days and abandoned drafts 30 days"
  }

  assert {
    condition     = aws_s3_bucket_lifecycle_configuration.workspaces[0].rule[0].noncurrent_version_expiration[0].noncurrent_days == 30
    error_message = "workspaces keep old versions 30 days"
  }

  assert {
    condition = flatten([
      for s in local.object_storage_policy.Statement : s.Resource if contains(s.Action, "s3:DeleteObject")
    ]) == ["arn:aws:s3:::ts-harness-skills/skills/drafts/*", "arn:aws:s3:::ts-harness-workspaces/*"]
    error_message = "the runtime may delete skill drafts and workspace files, never approved skill versions"
  }

  assert {
    condition     = [for s in local.object_storage_policy.Statement : s.Sid if contains(s.Action, "s3:GetObject")] == ["ReadWriteSkillsAndWorkspaces"]
    error_message = "the runtime reads and writes both buckets"
  }
}

run "object_storage_needs_postgres_state" {
  command = plan

  variables {
    object_backend = "s3"
    state_backend  = "local"
  }

  expect_failures = [
    terraform_data.storage_backends,
  ]
}

run "postgres_state_needs_rds" {
  command = plan

  variables {
    state_backend = "postgres"
  }

  expect_failures = [
    terraform_data.storage_backends,
  ]
}

run "scale_out_with_llm_proxy" {
  command = plan

  variables {
    deployment_profile = "scale_out"
    network_mode       = "vpc"
    memory_backend     = "rds"
    llm_proxy_enabled  = true
  }

  assert {
    condition     = flatten([for c in aws_lb_listener_rule.llm_proxy_route[0].condition : [for p in c.path_pattern : p.values]]) == ["/llm/*"]
    error_message = "the internal load balancer forwards /llm/* to the proxy"
  }

  assert {
    condition     = aws_lb_target_group.llm_proxy[0].port == 18795
    error_message = "the proxy listens on host port 18795"
  }

  assert {
    condition     = !contains(keys(local.coding_worker_secret_env), "CODING_BACKEND_API_KEY")
    error_message = "workers behind the proxy must not receive the provider key"
  }

  assert {
    condition     = local.runtime_config_object.LLM_PROXY_ENABLED == "true" && local.runtime_config_object.LLM_PROXY_HOST_PORT == "18795"
    error_message = "runtime config turns the proxy on and publishes its host port"
  }

  assert {
    condition     = contains(keys(local.all_secrets), "llm-proxy-secret")
    error_message = "the proxy token signing secret is provisioned"
  }
}
