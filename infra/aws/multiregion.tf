###############################################################################
# Multi-region configuration stub
#
# This file wires up a secondary AWS region, a Route 53 health-check-based
# failover record set, and an optional cross-region RDS read replica.
#
# Usage:
#   1. Set primary_region / secondary_region in terraform.tfvars.
#   2. Set enable_secondary_region = true.
#   3. Supply route53_zone_id and route53_record_name.
#   4. Set enable_rds_cross_region_replica = true if you need a read replica.
###############################################################################

###############################################################################
# Variables
###############################################################################

variable "primary_region" {
  description = "Primary AWS region (must match var.aws_region in the root module)"
  type        = string
  default     = "us-east-1"
}

variable "secondary_region" {
  description = "Secondary AWS region for failover"
  type        = string
  default     = "us-west-2"
}

variable "enable_secondary_region" {
  description = "When true, deploy resources in the secondary region and create Route 53 failover routing"
  type        = bool
  default     = false
}

variable "route53_zone_id" {
  description = "Route 53 hosted zone ID for the failover DNS record"
  type        = string
  default     = ""
}

variable "route53_record_name" {
  description = "Fully-qualified DNS name for the Route 53 failover record (e.g. api.example.com)"
  type        = string
  default     = ""
}

variable "primary_alb_dns_name" {
  description = "DNS name of the primary region ALB (used as failover PRIMARY target)"
  type        = string
  default     = ""
}

variable "primary_alb_zone_id" {
  description = "Hosted zone ID of the primary ALB (for Route 53 alias target)"
  type        = string
  default     = ""
}

variable "secondary_alb_dns_name" {
  description = "DNS name of the secondary region ALB (used as failover SECONDARY target)"
  type        = string
  default     = ""
}

variable "secondary_alb_zone_id" {
  description = "Hosted zone ID of the secondary ALB"
  type        = string
  default     = ""
}

variable "enable_rds_cross_region_replica" {
  description = "When true, create an RDS read replica in the secondary region (requires memory_backend = 'rds' and a source RDS ARN)"
  type        = bool
  default     = false
}

variable "rds_source_db_arn" {
  description = "ARN of the source RDS instance in the primary region (required when enable_rds_cross_region_replica = true)"
  type        = string
  default     = ""
}

variable "rds_replica_instance_class" {
  description = "Instance class for the cross-region RDS read replica"
  type        = string
  default     = "db.t3.micro"
}

###############################################################################
# Secondary region provider
###############################################################################

provider "aws" {
  alias  = "secondary"
  region = var.secondary_region
}

###############################################################################
# Route 53 health check – primary endpoint
###############################################################################

resource "aws_route53_health_check" "primary" {
  count = var.enable_secondary_region && var.route53_zone_id != "" && var.primary_alb_dns_name != "" ? 1 : 0

  fqdn              = var.primary_alb_dns_name
  port              = 443
  type              = "HTTPS"
  resource_path     = "/healthz"
  failure_threshold = 3
  request_interval  = 30

  tags = {
    Name    = "${var.project_name}-primary-hc"
    Project = var.project_name
  }
}

###############################################################################
# Route 53 failover records (stub – fill in ALB zone IDs to activate)
###############################################################################

resource "aws_route53_record" "primary" {
  count = var.enable_secondary_region && var.route53_zone_id != "" && var.primary_alb_dns_name != "" ? 1 : 0

  zone_id        = var.route53_zone_id
  name           = var.route53_record_name
  type           = "A"
  set_identifier = "primary"

  failover_routing_policy {
    type = "PRIMARY"
  }

  health_check_id = aws_route53_health_check.primary[0].id

  alias {
    name                   = var.primary_alb_dns_name
    zone_id                = var.primary_alb_zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "secondary" {
  count = var.enable_secondary_region && var.route53_zone_id != "" && var.secondary_alb_dns_name != "" ? 1 : 0

  zone_id        = var.route53_zone_id
  name           = var.route53_record_name
  type           = "A"
  set_identifier = "secondary"

  failover_routing_policy {
    type = "SECONDARY"
  }

  alias {
    name                   = var.secondary_alb_dns_name
    zone_id                = var.secondary_alb_zone_id
    evaluate_target_health = true
  }
}

###############################################################################
# Cross-region RDS read replica (stub)
###############################################################################

resource "aws_db_instance" "cross_region_replica" {
  count = var.enable_rds_cross_region_replica && var.rds_source_db_arn != "" ? 1 : 0

  provider = aws.secondary

  identifier          = "${var.project_name}-replica"
  replicate_source_db = var.rds_source_db_arn
  instance_class      = var.rds_replica_instance_class
  publicly_accessible = false
  skip_final_snapshot = true
  storage_encrypted   = true

  tags = {
    Name    = "${var.project_name}-rds-replica"
    Project = var.project_name
    Role    = "read-replica"
  }
}

###############################################################################
# Outputs
###############################################################################

output "route53_primary_record_fqdn" {
  description = "FQDN of the Route 53 PRIMARY failover record"
  value       = length(aws_route53_record.primary) > 0 ? aws_route53_record.primary[0].fqdn : ""
}

output "route53_secondary_record_fqdn" {
  description = "FQDN of the Route 53 SECONDARY failover record"
  value       = length(aws_route53_record.secondary) > 0 ? aws_route53_record.secondary[0].fqdn : ""
}

output "rds_replica_endpoint" {
  description = "Cross-region RDS replica endpoint (empty when not enabled)"
  value       = length(aws_db_instance.cross_region_replica) > 0 ? aws_db_instance.cross_region_replica[0].address : ""
}
