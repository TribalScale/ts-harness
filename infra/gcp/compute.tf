# Ubuntu 24.04 GCE VM + startup script (scripts/gcp-startup.sh).

data "google_compute_image" "ubuntu" {
  family  = "ubuntu-2404-lts-amd64"
  project = "ubuntu-os-cloud"
}

locals {
  # All __TF_TOKEN__ -> value mappings (parity with infra/aws/ec2.tf).
  _ud_subs = {
    "__TF_ADMIN_USER__"                    = var.admin_username
    "__TF_AGENT_RUNTIME_VERSION__"         = var.agent_runtime_version
    "__TF_GCP_PROJECT_ID__"                = var.gcp_project_id
    "__TF_CLOUD_PROVIDER__"                = "gcp"
    "__TF_CLOUD_REGION__"                  = var.cloud_region
    "__TF_CLOUD_ZONE__"                    = var.cloud_zone
    "__TF_SECRETS_PREFIX__"                = local.secret_prefix_effective
    "__TF_ENTERPRISE_REPO__"               = var.enterprise_repo_url
    "__TF_MEMORY_BACKEND__"                = var.memory_backend
    "__TF_MANAGED_DB_HOST__"               = local.managed_db_host_rendered
    "__TF_MANAGED_DB_PORT__"               = local.sql_db_port_rendered
    "__TF_MANAGED_DB_NAME__"               = local.sql_db_name_rendered
    "__TF_MANAGED_DB_USER__"               = local.sql_db_user_rendered
    "__TF_MANAGED_DB_CREDENTIAL_REF__"     = local.managed_db_credential_ref_rendered
    "__TF_CREDENTIALS_ENCRYPTION_KEY_ID__" = local.credentials_encryption_key_id_effective
    "__TF_INSTANCE_RESOURCE_ID__"          = ""
    "__TF_DATA_DISK_DEVICE_HINT__"         = local.data_disk_device_hint
  }

  _ud_s0  = file("${path.module}/../../scripts/gcp-startup.sh")
  _ud_s1  = replace(local._ud_s0, "__TF_ADMIN_USER__", local._ud_subs["__TF_ADMIN_USER__"])
  _ud_s2  = replace(local._ud_s1, "__TF_AGENT_RUNTIME_VERSION__", local._ud_subs["__TF_AGENT_RUNTIME_VERSION__"])
  _ud_s3  = replace(local._ud_s2, "__TF_GCP_PROJECT_ID__", local._ud_subs["__TF_GCP_PROJECT_ID__"])
  _ud_s4  = replace(local._ud_s3, "__TF_CLOUD_PROVIDER__", local._ud_subs["__TF_CLOUD_PROVIDER__"])
  _ud_s5  = replace(local._ud_s4, "__TF_CLOUD_REGION__", local._ud_subs["__TF_CLOUD_REGION__"])
  _ud_s6  = replace(local._ud_s5, "__TF_CLOUD_ZONE__", local._ud_subs["__TF_CLOUD_ZONE__"])
  _ud_s7  = replace(local._ud_s6, "__TF_SECRETS_PREFIX__", local._ud_subs["__TF_SECRETS_PREFIX__"])
  _ud_s8  = replace(local._ud_s7, "__TF_ENTERPRISE_REPO__", local._ud_subs["__TF_ENTERPRISE_REPO__"])
  _ud_s9  = replace(local._ud_s8, "__TF_MEMORY_BACKEND__", local._ud_subs["__TF_MEMORY_BACKEND__"])
  _ud_s10 = replace(local._ud_s9, "__TF_MANAGED_DB_HOST__", local._ud_subs["__TF_MANAGED_DB_HOST__"])
  _ud_s11 = replace(local._ud_s10, "__TF_MANAGED_DB_PORT__", local._ud_subs["__TF_MANAGED_DB_PORT__"])
  _ud_s12 = replace(local._ud_s11, "__TF_MANAGED_DB_NAME__", local._ud_subs["__TF_MANAGED_DB_NAME__"])
  _ud_s13 = replace(local._ud_s12, "__TF_MANAGED_DB_USER__", local._ud_subs["__TF_MANAGED_DB_USER__"])
  _ud_s14 = replace(local._ud_s13, "__TF_MANAGED_DB_CREDENTIAL_REF__", local._ud_subs["__TF_MANAGED_DB_CREDENTIAL_REF__"])
  _ud_s15 = replace(local._ud_s14, "__TF_CREDENTIALS_ENCRYPTION_KEY_ID__", local._ud_subs["__TF_CREDENTIALS_ENCRYPTION_KEY_ID__"])
  _ud_s16 = replace(local._ud_s15, "__TF_INSTANCE_RESOURCE_ID__", local._ud_subs["__TF_INSTANCE_RESOURCE_ID__"])
  _ud_s17 = replace(local._ud_s16, "__TF_DATA_DISK_DEVICE_HINT__", local._ud_subs["__TF_DATA_DISK_DEVICE_HINT__"])

  startup_script_rendered = local._ud_s17
}

resource "google_compute_instance" "harness" {
  name         = var.project_name
  machine_type = var.vm_instance_type
  zone         = var.cloud_zone
  tags         = ["${var.project_name}-vm"]

  boot_disk {
    initialize_params {
      image = data.google_compute_image.ubuntu.self_link
      size  = var.vm_root_volume_size_gb
      type  = "pd-balanced"
      labels = {
        name = "${var.project_name}-root"
      }
    }
  }

  attached_disk {
    source      = google_compute_disk.data.id
    device_name = local.data_disk_device_name
    mode        = "READ_WRITE"
  }

  network_interface {
    subnetwork = google_compute_subnetwork.main.id

    dynamic "access_config" {
      for_each = var.assign_public_ip ? [1] : []
      content {
        // Ephemeral public IP for bootstrap/SSH; app ports remain closed.
      }
    }
  }

  service_account {
    email  = google_service_account.runtime.email
    scopes = ["cloud-platform"]
  }

  metadata = {
    enable-oslogin = "FALSE"
    ssh-keys       = "${var.admin_username}:${var.ssh_public_key}"
  }

  metadata_startup_script = local.startup_script_rendered

  labels = {
    name                  = var.project_name
    agent_runtime_version = replace(var.agent_runtime_version, ".", "-")
  }

  allow_stopping_for_update = true

  lifecycle {
    ignore_changes = [metadata_startup_script]
  }

  depends_on = [
    google_project_service.required,
    google_project_iam_member.secretmanager_accessor,
    google_project_iam_member.monitoring_writer,
  ]
}
