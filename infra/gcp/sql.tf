# Cloud SQL PostgreSQL 16 — only when memory_backend = "managed".
#
# Default: private IP via Service Networking (preferred).
# Fallback: public IP with authorized_networks when cloud_sql_private_ip = false.
#
# After the database is created, connect and enable pgvector manually:
#   psql -h <host> -U ts_harness -d ts_harness
#   CREATE EXTENSION IF NOT EXISTS vector;

resource "random_password" "sql" {
  count = local.create_sql

  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "google_sql_database_instance" "harness" {
  count = local.create_sql

  name             = "${var.project_name}-pg"
  database_version = "POSTGRES_16"
  region           = var.cloud_region

  deletion_protection = true

  settings {
    tier              = var.managed_db_instance_class
    availability_type = var.cloud_sql_availability_type
    disk_size         = 20
    disk_type         = "PD_SSD"
    disk_autoresize   = true

    backup_configuration {
      enabled                        = true
      start_time                     = "03:00"
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = min(var.managed_db_backup_retention_days, 7)
      backup_retention_settings {
        retained_backups = var.managed_db_backup_retention_days
        retention_unit   = "COUNT"
      }
    }

    ip_configuration {
      ipv4_enabled                                  = !var.cloud_sql_private_ip
      private_network                               = var.cloud_sql_private_ip ? google_compute_network.main.id : null
      enable_private_path_for_google_cloud_services = var.cloud_sql_private_ip
      ssl_mode                                      = "ENCRYPTED_ONLY"

      dynamic "authorized_networks" {
        for_each = var.cloud_sql_private_ip ? [] : var.cloud_sql_authorized_networks
        content {
          name  = authorized_networks.value.name
          value = authorized_networks.value.cidr
        }
      }
    }

    user_labels = {
      name = "${var.project_name}-db"
    }
  }

  depends_on = [
    google_project_service.required,
    google_service_networking_connection.private_vpc,
  ]
}

resource "google_sql_database" "harness" {
  count = local.create_sql

  name     = var.managed_db_name
  instance = google_sql_database_instance.harness[0].name
}

resource "google_sql_user" "harness" {
  count = local.create_sql

  name     = var.managed_db_user
  instance = google_sql_database_instance.harness[0].name
  password = random_password.sql[0].result
}

# Store Cloud SQL credentials in Secret Manager (RDS-compatible JSON shape for adapters).
resource "google_secret_manager_secret" "sql_credentials" {
  count = local.create_sql

  secret_id = "${local.secret_prefix_effective}-managed-db-credentials"

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "sql_credentials" {
  count = local.create_sql

  secret = google_secret_manager_secret.sql_credentials[0].id
  secret_data = jsonencode({
    username = var.managed_db_user
    password = random_password.sql[0].result
    host     = var.cloud_sql_private_ip ? google_sql_database_instance.harness[0].private_ip_address : google_sql_database_instance.harness[0].public_ip_address
    port     = 5432
    dbname   = var.managed_db_name
    engine   = "postgres"
  })

  lifecycle {
    ignore_changes = [secret_data]
  }
}

locals {
  managed_db_host_rendered = local.create_sql == 1 ? (
    var.cloud_sql_private_ip
    ? google_sql_database_instance.harness[0].private_ip_address
    : google_sql_database_instance.harness[0].public_ip_address
  ) : ""
  managed_db_credential_ref_rendered = local.create_sql == 1 ? google_secret_manager_secret.sql_credentials[0].id : ""
}
