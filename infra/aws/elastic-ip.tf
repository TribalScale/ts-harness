resource "aws_eip" "harness" {
  domain = "vpc"

  tags = {
    Name = var.project_name
  }
}

resource "aws_eip_association" "harness" {
  instance_id   = aws_instance.harness.id
  allocation_id = aws_eip.harness.id
}
