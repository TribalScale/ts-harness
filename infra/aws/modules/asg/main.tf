###############################################################################
# ASG module – Auto Scaling Group for TS Harness agent-runtime
###############################################################################

variable "project_name" {
  description = "Name prefix for all resources"
  type        = string
  default     = "ts-harness"
}

variable "vpc_id" {
  description = "VPC ID"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for the ASG instances"
  type        = list(string)
}

variable "target_group_arns" {
  description = "ALB target group ARNs to register instances with"
  type        = list(string)
  default     = []
}

variable "ami_id" {
  description = "AMI ID for launch template (Ubuntu 24.04 recommended)"
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.medium"
}

variable "key_name" {
  description = "EC2 key pair name for SSH access"
  type        = string
  default     = ""
}

variable "iam_instance_profile_arn" {
  description = "IAM instance profile ARN to attach to instances"
  type        = string
  default     = ""
}

variable "alb_security_group_id" {
  description = "ALB security group ID (allows inbound from ALB)"
  type        = string
  default     = ""
}

variable "user_data_base64" {
  description = "Base64-encoded user-data script"
  type        = string
  default     = ""
}

variable "min_size" {
  description = "Minimum number of instances in the ASG"
  type        = number
  default     = 1
}

variable "max_size" {
  description = "Maximum number of instances in the ASG"
  type        = number
  default     = 3
}

variable "desired_capacity" {
  description = "Desired number of instances in the ASG"
  type        = number
  default     = 1
}

variable "health_check_grace_period" {
  description = "Seconds before ASG starts checking instance health"
  type        = number
  default     = 120
}

variable "tags" {
  description = "Additional tags"
  type        = map(string)
  default     = {}
}

###############################################################################
# Security group for instances
###############################################################################

resource "aws_security_group" "instance" {
  name        = "${var.project_name}-instance-sg"
  description = "TS Harness instance security group"
  vpc_id      = var.vpc_id

  dynamic "ingress" {
    for_each = var.alb_security_group_id != "" ? [1] : []
    content {
      from_port       = 18789
      to_port         = 18789
      protocol        = "tcp"
      security_groups = [var.alb_security_group_id]
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(
    { Name = "${var.project_name}-instance-sg" },
    var.tags,
  )
}

###############################################################################
# Launch template
###############################################################################

resource "aws_launch_template" "this" {
  name_prefix   = "${var.project_name}-"
  image_id      = var.ami_id
  instance_type = var.instance_type

  dynamic "key_name" {
    for_each = var.key_name != "" ? [var.key_name] : []
    content {
      # Terraform doesn't support dynamic block inside key_name attribute;
      # use conditional assignment via null.
    }
  }

  key_name = var.key_name != "" ? var.key_name : null

  vpc_security_group_ids = [aws_security_group.instance.id]

  dynamic "iam_instance_profile" {
    for_each = var.iam_instance_profile_arn != "" ? [1] : []
    content {
      arn = var.iam_instance_profile_arn
    }
  }

  dynamic "user_data" {
    for_each = var.user_data_base64 != "" ? [1] : []
    content {
      # user_data is set as a top-level attribute below
    }
  }

  user_data = var.user_data_base64 != "" ? var.user_data_base64 : null

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  tag_specifications {
    resource_type = "instance"
    tags = merge(
      { Name = var.project_name },
      var.tags,
    )
  }

  tag_specifications {
    resource_type = "volume"
    tags = merge(
      { Name = "${var.project_name}-vol" },
      var.tags,
    )
  }

  lifecycle {
    create_before_destroy = true
  }
}

###############################################################################
# Auto Scaling Group
###############################################################################

resource "aws_autoscaling_group" "this" {
  name                      = "${var.project_name}-asg"
  min_size                  = var.min_size
  max_size                  = var.max_size
  desired_capacity          = var.desired_capacity
  vpc_zone_identifier       = var.private_subnet_ids
  target_group_arns         = var.target_group_arns
  health_check_type         = length(var.target_group_arns) > 0 ? "ELB" : "EC2"
  health_check_grace_period = var.health_check_grace_period

  launch_template {
    id      = aws_launch_template.this.id
    version = "$Latest"
  }

  dynamic "tag" {
    for_each = merge({ Name = var.project_name }, var.tags)
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }

  lifecycle {
    create_before_destroy = true
    ignore_changes        = [desired_capacity]
  }
}

###############################################################################
# CPU-based scaling policy
###############################################################################

resource "aws_autoscaling_policy" "cpu" {
  name                   = "${var.project_name}-cpu-scaling"
  autoscaling_group_name = aws_autoscaling_group.this.name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ASGAverageCPUUtilization"
    }
    target_value = 70.0
  }
}

###############################################################################
# Outputs
###############################################################################

output "autoscaling_group_name" {
  description = "ASG name"
  value       = aws_autoscaling_group.this.name
}

output "launch_template_id" {
  description = "Launch template ID"
  value       = aws_launch_template.this.id
}

output "instance_security_group_id" {
  description = "Instance security group ID"
  value       = aws_security_group.instance.id
}
