# Dedicated data Persistent Disk mounted at /vol/ts-harness-data.
# Attached with a stable device_name so the by-id path is predictable.

resource "google_compute_disk" "data" {
  name = "${var.project_name}-data"
  type = "pd-balanced"
  zone = var.cloud_zone
  size = var.vm_data_disk_size_gb

  labels = {
    name = "${var.project_name}-data"
  }

  depends_on = [google_project_service.required]
}
