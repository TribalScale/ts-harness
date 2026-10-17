resource "aws_ebs_volume" "harness_data" {
  availability_zone = aws_instance.harness.availability_zone
  size              = var.ebs_size
  type              = "gp3"
  encrypted         = true

  tags = {
    Name = "${var.project_name}-data"
  }
}

resource "aws_volume_attachment" "harness_data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.harness_data.id
  instance_id = aws_instance.harness.id
}
