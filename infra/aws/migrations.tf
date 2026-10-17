# State-preserving migration notes for terraform/ → infra/aws/
#
# Resource addresses are unchanged (same module structure under the new
# working directory). Prefer changing the Terraform working directory and
# re-initializing against the existing backend rather than rewriting state.
#
# Add `moved` blocks below ONLY if a future change renames a resource address.
# Example (do not uncomment unless an address actually changes):
#
# moved {
#   from = aws_instance.legacy_name
#   to   = aws_instance.harness
# }
