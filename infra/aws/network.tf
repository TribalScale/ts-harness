# network_mode = "default" keeps the host and RDS in the account's default VPC.
# network_mode = "vpc" builds modules/vpc and places the host in a public
# subnet, RDS and coding workers in private subnets behind one NAT gateway.
# Switching an existing deployment from default to vpc replaces the host,
# its security group and the RDS subnet group; plan a maintenance window.

locals {
  use_vpc   = var.network_mode == "vpc"
  scale_out = var.deployment_profile == "scale_out"
  # The Fargate coding worker stack (cluster, task definition, ECR, internal
  # load balancer). scale_out builds it in the dedicated VPC; coding_workers_on_ecs
  # builds it on any profile, in the default VPC when network_mode = default.
  ecs_enabled = (local.scale_out && local.use_vpc) || var.coding_workers_on_ecs
  # Private subnets: VPC endpoints and the egress proxy, no public IPs.
  ecs_private = local.ecs_enabled && local.use_vpc
  # Default VPC: workers take a public IP for outbound traffic and reach AWS
  # APIs, GitHub and model providers directly on 80/443.
  ecs_default_vpc = local.ecs_enabled && !local.use_vpc
  ecs_vpc_id      = local.use_vpc ? local.vpc_id : one(data.aws_vpc.default[*].id)
  ecs_subnet_ids  = local.use_vpc ? local.private_subnet_ids : one(data.aws_subnets.coding_workers[*].ids)

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
  count       = local.ecs_private ? 1 : 0
  name        = "${var.project_name}-vpc-endpoints"
  description = "HTTPS from coding workers and the egress proxy to AWS interface endpoints"
  vpc_id      = local.vpc_id

  tags = {
    Name = "${var.project_name}-vpc-endpoints"
  }
}

resource "aws_vpc_security_group_ingress_rule" "vpc_endpoints_from_workers" {
  count = local.ecs_private ? 1 : 0

  security_group_id            = aws_security_group.vpc_endpoints[0].id
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.coding_worker[0].id
  description                  = "HTTPS from coding workers"
}

resource "aws_vpc_security_group_ingress_rule" "vpc_endpoints_from_proxy" {
  count = local.ecs_private ? 1 : 0

  security_group_id            = aws_security_group.vpc_endpoints[0].id
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.egress_proxy[0].id
  description                  = "HTTPS from the egress proxy"
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.ecs_private ? toset(["ecr.api", "ecr.dkr", "secretsmanager", "logs", "ssm"]) : toset([])

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
  count = local.ecs_private ? 1 : 0

  vpc_id            = local.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = module.vpc[*].private_route_table_id

  tags = {
    Name = "${var.project_name}-s3"
  }
}

data "aws_prefix_list" "s3" {
  count = local.ecs_private ? 1 : 0
  name  = "com.amazonaws.${var.aws_region}.s3"
}

# Default-VPC subnets for Fargate workers and their load balancer
# (coding_workers_on_ecs with network_mode = default).
data "aws_availability_zones" "coding_workers" {
  count            = local.ecs_default_vpc ? 1 : 0
  state            = "available"
  exclude_zone_ids = var.coding_ecs_excluded_zone_ids
}

data "aws_subnets" "coding_workers" {
  count = local.ecs_default_vpc ? 1 : 0

  filter {
    name   = "vpc-id"
    values = [one(data.aws_vpc.default[*].id)]
  }

  filter {
    name   = "default-for-az"
    values = ["true"]
  }

  filter {
    name   = "availability-zone-id"
    values = data.aws_availability_zones.coding_workers[0].zone_ids
  }
}

resource "terraform_data" "coding_launcher_ecs" {
  count = var.coding_launcher == "ecs" ? 1 : 0

  lifecycle {
    precondition {
      condition     = local.ecs_enabled
      error_message = "coding_launcher = ecs needs the Fargate stack: set coding_workers_on_ecs = true, or scale_out with network_mode = vpc."
    }
  }
}
