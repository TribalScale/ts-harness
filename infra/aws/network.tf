# network_mode = "default" keeps the host and RDS in the account's default VPC.
# network_mode = "vpc" builds modules/vpc and places the host in a public
# subnet, RDS and coding workers in private subnets behind one NAT gateway.
# Switching an existing deployment from default to vpc replaces the host,
# its security group and the RDS subnet group; plan a maintenance window.

locals {
  use_vpc   = var.network_mode == "vpc"
  scale_out = var.deployment_profile == "scale_out"
  # ECS, the egress proxy and the internal load balancer need private subnets.
  ecs_enabled = local.scale_out && local.use_vpc

  vpc_id             = one(module.vpc[*].vpc_id)
  host_subnet_id     = try(module.vpc[0].public_subnet_ids[0], null)
  private_subnet_ids = try(module.vpc[0].private_subnet_ids, [])
}

resource "terraform_data" "scale_out_network" {
  count = local.scale_out ? 1 : 0

  lifecycle {
    precondition {
      condition     = local.use_vpc
      error_message = "deployment_profile = scale_out requires network_mode = vpc."
    }
  }
}

data "aws_availability_zones" "available" {
  count = local.use_vpc ? 1 : 0
  state = "available"
}

module "vpc" {
  source = "./modules/vpc"
  count  = local.use_vpc ? 1 : 0

  project_name       = var.project_name
  vpc_cidr           = var.vpc_cidr
  availability_zones = slice(data.aws_availability_zones.available[0].names, 0, 3)
}

# ---------------------------------------------------------------------------
# VPC endpoints so Fargate workers pull images, read secrets and ship logs
# without general internet egress.
# ---------------------------------------------------------------------------

resource "aws_security_group" "vpc_endpoints" {
  count       = local.ecs_enabled ? 1 : 0
  name        = "${var.project_name}-vpc-endpoints"
  description = "HTTPS from coding workers and the egress proxy to AWS interface endpoints"
  vpc_id      = local.vpc_id

  tags = {
    Name = "${var.project_name}-vpc-endpoints"
  }
}

resource "aws_vpc_security_group_ingress_rule" "vpc_endpoints_from_workers" {
  count = local.ecs_enabled ? 1 : 0

  security_group_id            = aws_security_group.vpc_endpoints[0].id
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.coding_worker[0].id
  description                  = "HTTPS from coding workers"
}

resource "aws_vpc_security_group_ingress_rule" "vpc_endpoints_from_proxy" {
  count = local.ecs_enabled ? 1 : 0

  security_group_id            = aws_security_group.vpc_endpoints[0].id
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.egress_proxy[0].id
  description                  = "HTTPS from the egress proxy"
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.ecs_enabled ? toset(["ecr.api", "ecr.dkr", "secretsmanager", "logs", "ssm"]) : toset([])

  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = local.private_subnet_ids
  security_group_ids  = [aws_security_group.vpc_endpoints[0].id]
  private_dns_enabled = true

  tags = {
    Name = "${var.project_name}-${replace(each.key, ".", "-")}"
  }
}

resource "aws_vpc_endpoint" "s3" {
  count = local.ecs_enabled ? 1 : 0

  vpc_id            = local.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = module.vpc[*].private_route_table_id

  tags = {
    Name = "${var.project_name}-s3"
  }
}

data "aws_prefix_list" "s3" {
  count = local.ecs_enabled ? 1 : 0
  name  = "com.amazonaws.${var.aws_region}.s3"
}
