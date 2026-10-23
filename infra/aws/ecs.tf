# Fargate coding workers (deployment_profile = scale_out). The coding launcher
# on the host calls ecs:RunTask per coding job; each task clones, runs OpenCode
# and reports back to the memory API through the internal load balancer.

locals {
  coding_worker_container = "coding-worker"
  coding_worker_family    = "${var.project_name}-coding-worker"
  coding_worker_image = local.ecs_enabled ? format(
    "%s:%s",
    aws_ecr_repository.coding_worker[0].repository_url,
    var.coding_worker_image_tag,
  ) : ""
  coding_worker_secret_env = {
    CODING_BACKEND_API_KEY  = "coding-backend-api-key"
    CODING_BACKEND_PROVIDER = "coding-backend-provider"
    CODING_BACKEND_MODEL    = "coding-backend-model"
    GITHUB_TOKEN            = "github-token"
  }
  coding_worker_secret_arns = [
    for name in values(local.coding_worker_secret_env) : aws_secretsmanager_secret.harness[name].arn
  ]
  egress_proxy_url = local.ecs_enabled ? "http://egress-proxy.${var.project_name}.internal:3128" : ""
}

resource "aws_ecs_cluster" "coding" {
  count = local.ecs_enabled ? 1 : 0
  name  = "${var.project_name}-coding"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Name = "${var.project_name}-coding"
  }
}

resource "aws_ecs_cluster_capacity_providers" "coding" {
  count              = local.ecs_enabled ? 1 : 0
  cluster_name       = aws_ecs_cluster.coding[0].name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

resource "aws_cloudwatch_log_group" "coding_jobs" {
  count             = local.ecs_enabled ? 1 : 0
  name              = "/${var.project_name}/coding-jobs"
  retention_in_days = var.coding_worker_log_retention_days
}

# ---------------------------------------------------------------------------
# IAM: execution role pulls the image and resolves secrets; the task role
# grants the running worker nothing beyond writing its own logs.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_tasks_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "coding_worker_execution" {
  count              = local.ecs_enabled ? 1 : 0
  name               = "${var.project_name}-coding-worker-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

resource "aws_iam_role_policy_attachment" "coding_worker_execution" {
  count      = local.ecs_enabled ? 1 : 0
  role       = aws_iam_role.coding_worker_execution[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "coding_worker_execution_secrets" {
  count = local.ecs_enabled ? 1 : 0

  statement {
    sid       = "ReadCodingWorkerSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = local.coding_worker_secret_arns
  }

  statement {
    sid       = "ReadEgressAllowlist"
    actions   = ["ssm:GetParameters"]
    resources = [aws_ssm_parameter.egress_extra_domains[0].arn]
  }
}

resource "aws_iam_role_policy" "coding_worker_execution_secrets" {
  count  = local.ecs_enabled ? 1 : 0
  name   = "${var.project_name}-coding-worker-secrets"
  role   = aws_iam_role.coding_worker_execution[0].id
  policy = data.aws_iam_policy_document.coding_worker_execution_secrets[0].json
}

resource "aws_iam_role" "coding_worker_task" {
  count              = local.ecs_enabled ? 1 : 0
  name               = "${var.project_name}-coding-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

data "aws_iam_policy_document" "coding_worker_task_logs" {
  count = local.ecs_enabled ? 1 : 0

  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.coding_jobs[0].arn}:*"]
  }
}

resource "aws_iam_role_policy" "coding_worker_task_logs" {
  count  = local.ecs_enabled ? 1 : 0
  name   = "${var.project_name}-coding-worker-logs"
  role   = aws_iam_role.coding_worker_task[0].id
  policy = data.aws_iam_policy_document.coding_worker_task_logs[0].json
}

# ---------------------------------------------------------------------------
# Worker network: no public IP, no ingress. Egress only to AWS endpoints,
# the egress proxy and the internal memory API load balancer.
# ---------------------------------------------------------------------------

resource "aws_security_group" "coding_worker" {
  count       = local.ecs_enabled ? 1 : 0
  name        = "${var.project_name}-coding-worker"
  description = "Fargate coding workers: egress to endpoints, proxy and memory API only"
  vpc_id      = local.vpc_id

  tags = {
    Name = "${var.project_name}-coding-worker"
  }
}

resource "aws_vpc_security_group_egress_rule" "coding_worker_to_endpoints" {
  count = local.ecs_enabled ? 1 : 0

  security_group_id            = aws_security_group.coding_worker[0].id
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.vpc_endpoints[0].id
  description                  = "AWS interface endpoints"
}

resource "aws_vpc_security_group_egress_rule" "coding_worker_to_s3" {
  count = local.ecs_enabled ? 1 : 0

  security_group_id = aws_security_group.coding_worker[0].id
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  prefix_list_id    = data.aws_prefix_list.s3[0].id
  description       = "S3 gateway endpoint (ECR image layers)"
}

resource "aws_vpc_security_group_egress_rule" "coding_worker_to_proxy" {
  count = local.ecs_enabled ? 1 : 0

  security_group_id            = aws_security_group.coding_worker[0].id
  from_port                    = 3128
  to_port                      = 3128
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.egress_proxy[0].id
  description                  = "Egress proxy"
}

resource "aws_vpc_security_group_egress_rule" "coding_worker_to_memory_api" {
  count = local.ecs_enabled ? 1 : 0

  security_group_id            = aws_security_group.coding_worker[0].id
  from_port                    = local.memory_api_lb_port
  to_port                      = local.memory_api_lb_port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.memory_api_lb[0].id
  description                  = "Internal memory API load balancer"
}

# ---------------------------------------------------------------------------
# Task definition. JOB_ID and JOB_TOKEN arrive as RunTask overrides.
# ---------------------------------------------------------------------------

resource "aws_ecs_task_definition" "coding_worker" {
  count                    = local.ecs_enabled ? 1 : 0
  family                   = local.coding_worker_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.coding_worker_cpu)
  memory                   = tostring(var.coding_worker_memory)
  execution_role_arn       = aws_iam_role.coding_worker_execution[0].arn
  task_role_arn            = aws_iam_role.coding_worker_task[0].arn

  ephemeral_storage {
    size_in_gib = var.coding_worker_ephemeral_storage_gib
  }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name        = local.coding_worker_container
      image       = local.coding_worker_image
      essential   = true
      stopTimeout = 30
      environment = [
        { name = "MEMORY_API_URL", value = local.memory_api_worker_url },
        { name = "HTTPS_PROXY", value = local.egress_proxy_url },
        { name = "HTTP_PROXY", value = local.egress_proxy_url },
        { name = "https_proxy", value = local.egress_proxy_url },
        { name = "http_proxy", value = local.egress_proxy_url },
        { name = "NO_PROXY", value = "${aws_lb.memory_api[0].dns_name},.amazonaws.com,169.254.170.2,localhost,127.0.0.1" },
        { name = "no_proxy", value = "${aws_lb.memory_api[0].dns_name},.amazonaws.com,169.254.170.2,localhost,127.0.0.1" },
      ]
      secrets = [
        for env_name, secret_name in local.coding_worker_secret_env : {
          name      = env_name
          valueFrom = aws_secretsmanager_secret.harness[secret_name].arn
        }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.coding_jobs[0].name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "job"
        }
      }
    }
  ])

  tags = {
    Name = local.coding_worker_family
  }
}
