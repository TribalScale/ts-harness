# VPC / subnet / firewall for the single-VM install.
# SSH on 2222 only; no public app ports (gateway stays private / Tailscale).

resource "google_compute_network" "main" {
  name                    = "${var.project_name}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [google_project_service.required]
}

resource "google_compute_subnetwork" "main" {
  name                     = "${var.project_name}-subnet"
  ip_cidr_range            = var.vpc_cidr
  region                   = var.cloud_region
  network                  = google_compute_network.main.id
  private_ip_google_access = true
}

# Private Service Access range for Cloud SQL private IP (preferred path).
resource "google_compute_global_address" "private_services" {
  count = local.create_sql == 1 && var.cloud_sql_private_ip ? 1 : 0

  name          = "${var.project_name}-psa"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.main.id

  depends_on = [google_project_service.required]
}

resource "google_service_networking_connection" "private_vpc" {
  count = local.create_sql == 1 && var.cloud_sql_private_ip ? 1 : 0

  network                 = google_compute_network.main.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services[0].name]

  depends_on = [google_project_service.required]
}

resource "google_compute_firewall" "ssh" {
  name    = "${var.project_name}-ssh-2222"
  network = google_compute_network.main.name

  direction = "INGRESS"
  priority  = 1000

  allow {
    protocol = "tcp"
    ports    = ["2222"]
  }

  source_ranges = var.ssh_allowed_cidrs
  target_tags   = ["${var.project_name}-vm"]

  description = "SSH on port 2222 from operator CIDRs (no public app ports)"
}

resource "google_compute_firewall" "allow_internal" {
  name    = "${var.project_name}-allow-internal"
  network = google_compute_network.main.name

  direction = "INGRESS"
  priority  = 65534

  allow {
    protocol = "tcp"
  }
  allow {
    protocol = "udp"
  }
  allow {
    protocol = "icmp"
  }

  source_ranges = [var.vpc_cidr]
  description   = "Allow intra-VPC traffic (Cloud SQL private IP path)"
}

resource "google_compute_firewall" "egress_all" {
  name      = "${var.project_name}-egress-all"
  network   = google_compute_network.main.name
  direction = "EGRESS"
  priority  = 1000

  allow {
    protocol = "all"
  }

  destination_ranges = ["0.0.0.0/0"]
  target_tags        = ["${var.project_name}-vm"]
  description        = "Allow all outbound traffic for bootstrap and runtime"
}
