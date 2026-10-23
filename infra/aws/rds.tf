# RDS resources are only provisioned when var.memory_backend == "rds".
#
# After the database is created, connect and enable pgvector manually:
#   psql -h <endpoint> -U ts_harness -d ts_harness
#   CREATE EXTENSION IF NOT EXISTS vector;

locals {
  create_rds = var.memory_backend == "rds" ? 1 : 0
}

# ---------------------------------------------------------------------------
# Networking — default VPC subnets for the DB subnet group
# ---------------------------------------------------------------------------

data "aws_vpc" "default" {
  count   = local.create_rds
  default = true
}

data "aws_subnets" "default" {
  count = local.create_rds

  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }
}

resource "aws_db_subnet_group" "harness" {
  count      = local.create_rds
  name       = var.project_name
  subnet_ids = data.aws_subnets.default[0].ids

  tags = {
    Name = "${var.project_name}-db"
  }
}

# ---------------------------------------------------------------------------
# Security group — PostgreSQL from EC2 only
# ---------------------------------------------------------------------------

resource "aws_security_group" "rds" {
  count       = local.create_rds
  name        = "${var.project_name}-rds"
  description = "Allow PostgreSQL from the TS Harness EC2 security group"
  vpc_id      = data.aws_vpc.default[0].id

  tags = {
    Name = "${var.project_name}-rds"
  }
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_ec2" {
  count = local.create_rds

  security_group_id            = aws_security_group.rds[0].id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.harness.id
  description                  = "PostgreSQL from TS Harness EC2"
}

# ---------------------------------------------------------------------------
# RDS PostgreSQL 16 instance
# ---------------------------------------------------------------------------

resource "aws_db_instance" "harness" {
  count = local.create_rds

  identifier     = var.project_name
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.rds_instance_class

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "ts_harness"
  username = "ts_harness"

  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.harness[0].name
  vpc_security_group_ids = [aws_security_group.rds[0].id]

  multi_az            = var.rds_multi_az
  apply_immediately   = var.rds_apply_immediately
  publicly_accessible = false

  backup_retention_period = var.rds_backup_retention_days
  backup_window           = var.rds_backup_window
  copy_tags_to_snapshot   = true
  deletion_protection     = true

  skip_final_snapshot       = var.rds_skip_final_snapshot
  final_snapshot_identifier = var.rds_skip_final_snapshot ? null : coalesce(var.rds_final_snapshot_identifier, "${var.project_name}-db-final")

  tags = {
    Name = "${var.project_name}-db"
  }
}
