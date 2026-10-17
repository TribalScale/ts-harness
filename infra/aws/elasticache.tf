# ElastiCache Redis for shared runtime state (dispatch cache, rate limits, budgets).

resource "aws_elasticache_subnet_group" "ts_harness" {
  count      = var.enable_redis ? 1 : 0
  name       = "${var.project_name}-redis-subnets"
  subnet_ids = var.redis_subnet_ids
}

resource "aws_security_group" "redis" {
  count       = var.enable_redis ? 1 : 0
  name        = "${var.project_name}-redis-sg"
  description = "ElastiCache Redis for TS Harness shared state"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.harness.id]
    description     = "Redis from agent runtime"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_elasticache_replication_group" "ts_harness" {
  count                      = var.enable_redis ? 1 : 0
  replication_group_id       = "${var.project_name}-redis"
  description                = "TS Harness shared state"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  num_cache_clusters         = var.redis_multi_az ? 2 : 1
  automatic_failover_enabled = var.redis_multi_az
  multi_az_enabled           = var.redis_multi_az
  subnet_group_name          = aws_elasticache_subnet_group.ts_harness[0].name
  security_group_ids         = [aws_security_group.redis[0].id]
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  port                       = 6379
}
