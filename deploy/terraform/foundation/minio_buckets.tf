# The datalake bucket is created by the minio-init hook job in the Helm chart
# on first deploy. This Terraform resource tracks the bucket's existence and
# prevents accidental destruction.
#
# Requires: terraform/services docker_compose_deploy applied first (MinIO CE running on VM).
# If migrating from AIStor: run `terraform state rm minio_s3_bucket.datalake` once
# to clear the old state entry before re-applying.

resource "minio_s3_bucket" "datalake" {
  depends_on = [null_resource.truenas_nfs_share]
  bucket     = "datalake"
  acl        = "private"
  lifecycle { prevent_destroy = true }
}
