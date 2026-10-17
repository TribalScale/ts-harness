# State / address migration notes for the GCP install root.
#
# This root is greenfield (no prior terraform/ GCP state). Add `moved` blocks
# below ONLY if a future change renames a resource address.
#
# Example (do not uncomment unless an address actually changes):
#
# moved {
#   from = google_compute_instance.legacy_name
#   to   = google_compute_instance.harness
# }
