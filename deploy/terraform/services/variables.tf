variable "vm_host" {
  type    = string
  default = "10.0.0.60"
}

variable "vm_user" {
  type    = string
  default = "openclaw"
}

variable "vm_password" {
  type      = string
  sensitive = true
}

# MinIO CE endpoint used by pipeline scripts running inside Airflow Docker container.
# Containers on the datalake network reach MinIO via the service name "minio".
# For manual host-level runs use localhost:9000.
variable "aistor_endpoint" {
  type    = string
  default = "minio:9000"
}

variable "aistor_access_key" {
  type      = string
  sensitive = true
}

variable "aistor_secret_key" {
  type      = string
  sensitive = true
}

variable "truenas_host" {
  type    = string
  default = "10.0.0.160"
}

# Address the Ubuntu VM uses to reach TrueNAS (direct ens4↔br0 link, not LAN)
variable "truenas_script_host" {
  type    = string
  default = "172.16.100.1"
}

# ZFS pool name — used to build the NFS mount path for the datalake volume
variable "zfs_pool" {
  type    = string
  default = "WB-RAID-Z-18TB"
}

variable "truenas_api_key" {
  type      = string
  sensitive = true
}

variable "jellyfin_api_key" {
  type      = string
  sensitive = true
}

variable "superset_admin_password" {
  type      = string
  sensitive = true
}

variable "jupyter_token" {
  type      = string
  sensitive = true
}

variable "airflow_password" {
  type      = string
  sensitive = true
}
