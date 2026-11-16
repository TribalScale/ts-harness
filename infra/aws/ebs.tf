# The data disk outlives the instance: replacing the instance (a rebuild) keeps /vol. The disk
# decides the zone and the instance follows it, so a replacement instance always lands where it
# can attach the disk. ignore_changes keeps an existing disk from ever being replaced over its
# zone. See docs/runbooks/rebuild-a-host.md.
data "aws_availability_zones" "host" {
  state = "available"
}

locals {
  # New installs: the zone of the VPC's first public subnet (or the region's first zone).
  data_disk_availability_zone = var.host_availability_zone != "" ? var.host_availability_zone : try(data.aws_availability_zones.available[0].names[0], data.aws_availability_zones.host.names[0])
}

resource "aws_ebs_volume" "harness_data" {
  availability_zone = local.data_disk_availability_zone
  size              = var.ebs_size
  type              = "gp3"
  encrypted         = true

  tags = {
    Name = "${var.project_name}-data"
  }

  lifecycle {
    ignore_changes = [availability_zone]
  }
}

resource "aws_volume_attachment" "harness_data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.harness_data.id
  instance_id = aws_instance.harness.id
}
