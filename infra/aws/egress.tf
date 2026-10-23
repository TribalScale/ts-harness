# Squid forward proxy on Fargate. Coding workers have no NAT route of their
# own; package installs, git and LLM calls go through this allowlist.
# Terraform owns the base list. Project registries are appended at runtime
# through the egress-extra-domains SSM parameter (Projects > Registries),
# which the proxy reads on start; a registry change forces a new deployment.

locals {
  egress_base_domains = distinct(concat([
    "github.com",
    ".github.com",
    ".githubusercontent.com",
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    "pypi.org",
    "files.pythonhosted.org",
    "api.anthropic.com",
    "api.openai.com",
    "openrouter.ai",
    "generativelanguage.googleapis.com",
    "models.dev",
    ".opencode.ai",
  ], var.egress_allowed_domains))

  egress_proxy_entrypoint = <<-EOT
    set -eu
    conf=/etc/squid/squid.conf
    {
      echo "http_port 3128"
      echo "pid_filename none"
      echo "cache deny all"
      echo "cache_log stdio:/dev/stderr"
      echo "access_log stdio:/dev/stdout"
      echo "logfile_rotate 0"
      echo "acl safe_ports port 80 443"
      echo "acl ssl_ports port 443"
      echo "acl CONNECT method CONNECT"
      for d in $(printf '%s,%s' "$EGRESS_BASE_DOMAINS" "$EGRESS_EXTRA_DOMAINS" | tr ',' ' '); do
        [ "$d" = "none" ] || echo "acl allowed dstdomain $d"
      done
      echo "http_access deny !safe_ports"
      echo "http_access deny CONNECT !ssl_ports"
      echo "http_access allow allowed"
      echo "http_access deny all"
    } > "$conf"
    exec squid -N -f "$conf"
  EOT
}

resource "aws_ssm_parameter" "egress_extra_domains" {
  count       = local.ecs_enabled ? 1 : 0
  name        = "/${local.secret_prefix_effective}/egress-extra-domains"
  description = "Comma-separated registry domains added from project settings; 'none' when empty."
  type        = "String"
  value       = "none"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_cloudwatch_log_group" "egress_proxy" {
  count             = local.ecs_enabled ? 1 : 0
  name              = "/${var.project_name}/egress-proxy"
  retention_in_days = var.coding_worker_log_retention_days
}

resource "aws_security_group" "egress_proxy" {
  count       = local.ecs_enabled ? 1 : 0
  name        = "${var.project_name}-egress-proxy"
  description = "Squid egress proxy for coding workers"
  vpc_id      = local.vpc_id

  tags = {
    Name = "${var.project_name}-egress-proxy"
  }
}

resource "aws_vpc_security_group_ingress_rule" "egress_proxy_from_workers" {
  count = local.ecs_enabled ? 1 : 0

  security_group_id            = aws_security_group.egress_proxy[0].id
  from_port                    = 3128
  to_port                      = 3128
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.coding_worker[0].id
  description                  = "Proxy port from coding workers"
}

resource "aws_vpc_security_group_egress_rule" "egress_proxy_https" {
  count = local.ecs_enabled ? 1 : 0

  security_group_id = aws_security_group.egress_proxy[0].id
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  cidr_ipv4         = "0.0.0.0/0"
  description       = "HTTPS to allowlisted domains"
}

resource "aws_vpc_security_group_egress_rule" "egress_proxy_http" {
  count = local.ecs_enabled ? 1 : 0

  security_group_id = aws_security_group.egress_proxy[0].id
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  cidr_ipv4         = "0.0.0.0/0"
  description       = "HTTP to allowlisted domains"
}

resource "aws_service_discovery_private_dns_namespace" "internal" {
  count = local.ecs_enabled ? 1 : 0
  name  = "${var.project_name}.internal"
  vpc   = local.vpc_id
}

resource "aws_service_discovery_service" "egress_proxy" {
  count = local.ecs_enabled ? 1 : 0
  name  = "egress-proxy"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.internal[0].id
    routing_policy = "MULTIVALUE"

    dns_records {
      type = "A"
      ttl  = 10
    }
  }
}

resource "aws_ecs_task_definition" "egress_proxy" {
  count                    = local.ecs_enabled ? 1 : 0
  family                   = "${var.project_name}-egress-proxy"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.coding_worker_execution[0].arn

  container_definitions = jsonencode([
    {
      name         = "squid"
      image        = var.egress_proxy_image
      essential    = true
      entryPoint   = ["/bin/sh", "-c"]
      command      = [local.egress_proxy_entrypoint]
      portMappings = [{ containerPort = 3128, protocol = "tcp" }]
      environment = [
        { name = "EGRESS_BASE_DOMAINS", value = join(",", local.egress_base_domains) },
      ]
      secrets = [
        { name = "EGRESS_EXTRA_DOMAINS", valueFrom = aws_ssm_parameter.egress_extra_domains[0].arn },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.egress_proxy[0].name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "squid"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "egress_proxy" {
  count           = local.ecs_enabled ? 1 : 0
  name            = "egress-proxy"
  cluster         = aws_ecs_cluster.coding[0].id
  task_definition = aws_ecs_task_definition.egress_proxy[0].arn
  desired_count   = 2
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = local.private_subnet_ids
    security_groups  = [aws_security_group.egress_proxy[0].id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.egress_proxy[0].arn
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}
