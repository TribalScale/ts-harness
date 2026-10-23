# No public TS Harness port exposed - Tailscale handles private network access.

resource "aws_security_group" "harness" {
  name        = "${var.project_name}-ec2"
  description = "TS Harness EC2 instance - SSH on 2222, all egress"
  vpc_id      = local.vpc_id

  tags = {
    Name = "${var.project_name}-ec2"
  }
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  for_each = toset(var.ssh_allowed_cidrs)

  security_group_id = aws_security_group.harness.id
  from_port         = 2222
  to_port           = 2222
  ip_protocol       = "tcp"
  cidr_ipv4         = each.value
  description       = "SSH from ${each.value}"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.harness.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
  description       = "Allow all outbound traffic"
}
