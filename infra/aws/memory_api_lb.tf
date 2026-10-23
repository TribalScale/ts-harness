# Internal load balancer so Fargate coding workers can reach the memory API on
# the host. Only the health check, the coding-job worker routes and the
# runtime's GitHub token route are forwarded; everything else gets a 404 at the
# load balancer.

locals {
  memory_api_port        = 18792
  memory_api_lb_protocol = var.memory_api_certificate_arn != "" ? "HTTPS" : "HTTP"
  memory_api_lb_port     = var.memory_api_certificate_arn != "" ? 443 : 80
  memory_api_worker_url = local.ecs_enabled ? format(
    "%s://%s",
    lower(local.memory_api_lb_protocol),
    aws_lb.memory_api[0].dns_name,
  ) : ""
}

resource "aws_security_group" "memory_api_lb" {
  count       = local.ecs_enabled ? 1 : 0
  name        = "${var.project_name}-memory-api-lb"
  description = "Internal memory API load balancer, reachable from coding workers only"
  vpc_id      = local.vpc_id

  tags = {
    Name = "${var.project_name}-memory-api-lb"
  }
}

resource "aws_vpc_security_group_ingress_rule" "memory_api_lb_from_workers" {
  count = local.ecs_enabled ? 1 : 0

  security_group_id            = aws_security_group.memory_api_lb[0].id
  from_port                    = local.memory_api_lb_port
  to_port                      = local.memory_api_lb_port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.coding_worker[0].id
  description                  = "Memory API from coding workers"
}

resource "aws_vpc_security_group_egress_rule" "memory_api_lb_to_host" {
  count = local.ecs_enabled ? 1 : 0

  security_group_id            = aws_security_group.memory_api_lb[0].id
  from_port                    = local.memory_api_port
  to_port                      = local.memory_api_port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.harness.id
  description                  = "Memory API on the host"
}

resource "aws_vpc_security_group_ingress_rule" "host_memory_api_from_lb" {
  count = local.ecs_enabled ? 1 : 0

  security_group_id            = aws_security_group.harness.id
  from_port                    = local.memory_api_port
  to_port                      = local.memory_api_port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.memory_api_lb[0].id
  description                  = "Memory API from the internal load balancer"
}

resource "aws_lb" "memory_api" {
  count              = local.ecs_enabled ? 1 : 0
  name               = "${var.project_name}-memory-api"
  internal           = true
  load_balancer_type = "application"
  security_groups    = [aws_security_group.memory_api_lb[0].id]
  subnets            = local.private_subnet_ids

  drop_invalid_header_fields = true
  # Non-streamed model calls through the LLM proxy can run for minutes.
  idle_timeout = var.llm_proxy_enabled ? 900 : 60

  tags = {
    Name = "${var.project_name}-memory-api"
  }
}

resource "aws_lb_target_group" "memory_api" {
  count       = local.ecs_enabled ? 1 : 0
  name        = "${var.project_name}-memory-api"
  port        = local.memory_api_port
  protocol    = "HTTP"
  vpc_id      = local.vpc_id
  target_type = "instance"

  health_check {
    path                = "/health"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = {
    Name = "${var.project_name}-memory-api"
  }
}

resource "aws_lb_target_group_attachment" "memory_api" {
  count            = local.ecs_enabled ? 1 : 0
  target_group_arn = aws_lb_target_group.memory_api[0].arn
  target_id        = aws_instance.harness.id
  port             = local.memory_api_port
}

resource "aws_lb_listener" "memory_api" {
  count             = local.ecs_enabled ? 1 : 0
  load_balancer_arn = aws_lb.memory_api[0].arn
  port              = local.memory_api_lb_port
  protocol          = local.memory_api_lb_protocol
  ssl_policy        = var.memory_api_certificate_arn != "" ? "ELBSecurityPolicy-TLS13-1-2-2021-06" : null
  certificate_arn   = var.memory_api_certificate_arn != "" ? var.memory_api_certificate_arn : null

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "application/json"
      message_body = "{\"error\":\"not_found\"}"
      status_code  = "404"
    }
  }
}

resource "aws_lb_listener_rule" "memory_api_worker_routes" {
  count        = local.ecs_enabled ? 1 : 0
  listener_arn = aws_lb_listener.memory_api[0].arn
  priority     = 10

  condition {
    path_pattern {
      values = ["/health", "/coding-jobs/*"]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.memory_api[0].arn
  }
}

# Workers refresh their repository-scoped GitHub token from the runtime.
locals {
  runtime_port = 18789
}

resource "aws_vpc_security_group_egress_rule" "memory_api_lb_to_runtime" {
  count = local.ecs_enabled ? 1 : 0

  security_group_id            = aws_security_group.memory_api_lb[0].id
  from_port                    = local.runtime_port
  to_port                      = local.runtime_port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.harness.id
  description                  = "Runtime token endpoint on the host"
}

resource "aws_vpc_security_group_ingress_rule" "host_runtime_from_lb" {
  count = local.ecs_enabled ? 1 : 0

  security_group_id            = aws_security_group.harness.id
  from_port                    = local.runtime_port
  to_port                      = local.runtime_port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.memory_api_lb[0].id
  description                  = "Runtime token endpoint from the internal load balancer"
}

resource "aws_lb_target_group" "runtime_tokens" {
  count       = local.ecs_enabled ? 1 : 0
  name        = "${var.project_name}-runtime-tokens"
  port        = local.runtime_port
  protocol    = "HTTP"
  vpc_id      = local.vpc_id
  target_type = "instance"

  health_check {
    path                = "/healthz"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = {
    Name = "${var.project_name}-runtime-tokens"
  }
}

resource "aws_lb_target_group_attachment" "runtime_tokens" {
  count            = local.ecs_enabled ? 1 : 0
  target_group_arn = aws_lb_target_group.runtime_tokens[0].arn
  target_id        = aws_instance.harness.id
  port             = local.runtime_port
}

resource "aws_lb_listener_rule" "runtime_token_route" {
  count        = local.ecs_enabled ? 1 : 0
  listener_arn = aws_lb_listener.memory_api[0].arn
  priority     = 20

  condition {
    path_pattern {
      values = ["/internal/coding-jobs/*/github-token"]
    }
  }

  condition {
    http_request_method {
      values = ["POST"]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.runtime_tokens[0].arn
  }
}

# Coding workers call the metering LLM proxy on the host instead of providers.
locals {
  llm_proxy_port       = 18795
  llm_proxy_lb_enabled = local.ecs_enabled && var.llm_proxy_enabled
  llm_proxy_worker_url = local.llm_proxy_lb_enabled ? "${local.memory_api_worker_url}/llm" : ""
}

resource "aws_vpc_security_group_egress_rule" "memory_api_lb_to_llm_proxy" {
  count = local.llm_proxy_lb_enabled ? 1 : 0

  security_group_id            = aws_security_group.memory_api_lb[0].id
  from_port                    = local.llm_proxy_port
  to_port                      = local.llm_proxy_port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.harness.id
  description                  = "LLM proxy on the host"
}

resource "aws_vpc_security_group_ingress_rule" "host_llm_proxy_from_lb" {
  count = local.llm_proxy_lb_enabled ? 1 : 0

  security_group_id            = aws_security_group.harness.id
  from_port                    = local.llm_proxy_port
  to_port                      = local.llm_proxy_port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.memory_api_lb[0].id
  description                  = "LLM proxy from the internal load balancer"
}

resource "aws_lb_target_group" "llm_proxy" {
  count       = local.llm_proxy_lb_enabled ? 1 : 0
  name        = "${var.project_name}-llm-proxy"
  port        = local.llm_proxy_port
  protocol    = "HTTP"
  vpc_id      = local.vpc_id
  target_type = "instance"

  health_check {
    path                = "/health"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = {
    Name = "${var.project_name}-llm-proxy"
  }
}

resource "aws_lb_target_group_attachment" "llm_proxy" {
  count            = local.llm_proxy_lb_enabled ? 1 : 0
  target_group_arn = aws_lb_target_group.llm_proxy[0].arn
  target_id        = aws_instance.harness.id
  port             = local.llm_proxy_port
}

resource "aws_lb_listener_rule" "llm_proxy_route" {
  count        = local.llm_proxy_lb_enabled ? 1 : 0
  listener_arn = aws_lb_listener.memory_api[0].arn
  priority     = 30

  condition {
    path_pattern {
      values = ["/llm/*"]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.llm_proxy[0].arn
  }
}
