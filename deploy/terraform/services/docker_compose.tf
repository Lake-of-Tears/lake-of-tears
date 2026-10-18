# Deploys all datalake services on the Ubuntu VM via Docker Compose.
# Replaces the individual jupyter.tf / superset.tf / airflow.tf resources.
#
# Pre-requisites:
#   1. docker_setup.tf has run (Docker installed)
#   2. ingestion_scripts.tf and embed_scripts.tf have run (scripts in ~/datalake)
#   3. terraform/foundation NFS share is active on TrueNAS

resource "null_resource" "docker_compose_deploy" {
  depends_on = [
    null_resource.docker_setup,
    null_resource.ingestion_scripts,
    null_resource.embed_scripts,
  ]

  triggers = {
    vm_host      = var.vm_host
    vm_user      = var.vm_user
    vm_password  = var.vm_password
    compose_hash = filemd5("${path.module}/../../docker/docker-compose.yml")
    # Rotate compose when any credential changes
    creds_hash = sha256(join(",", [
      var.aistor_access_key,
      var.aistor_secret_key,
      var.jupyter_token,
      var.superset_admin_password,
      var.airflow_password,
    ]))
  }

  # Uses self.triggers (not var.*) because Terraform requires connections that the destroy-time
  # provisioner can inherit to reference only the resource itself.
  connection {
    type     = "ssh"
    host     = self.triggers.vm_host
    user     = self.triggers.vm_user
    password = self.triggers.vm_password
  }

  # Step 1 — NFS: install nfs-common and mount TrueNAS ZFS dataset
  provisioner "remote-exec" {
    inline = [
      "echo '${var.vm_password}' | sudo -S apt-get install -y nfs-common -qq",
      "echo '${var.vm_password}' | sudo -S mkdir -p /mnt/datalake",
      # Add fstab entry once (idempotent)
      "grep -qF '${var.truenas_script_host}:/mnt/${var.zfs_pool}/datalake' /etc/fstab || echo '${var.truenas_script_host}:/mnt/${var.zfs_pool}/datalake /mnt/datalake nfs defaults,nofail,_netdev 0 0' | sudo tee -a /etc/fstab",
      "echo '${var.vm_password}' | sudo -S mount -a 2>/dev/null || true",
      "mountpoint -q /mnt/datalake || { echo 'ERROR: NFS mount failed — is the TrueNAS NFS share active?'; exit 1; }",
      "echo 'NFS mount OK: /mnt/datalake'",
    ]
  }

  # Step 2 — copy docker-compose.yml to the VM
  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "sshpass -p '${var.vm_password}' scp -o StrictHostKeyChecking=no ${path.module}/../../docker/docker-compose.yml ${var.vm_user}@${var.vm_host}:$HOME/docker-compose.yml"
  }

  # Step 3 — write .env with actual credentials (600 perms)
  provisioner "remote-exec" {
    inline = [
      "cat > $HOME/.datalake.env <<'ENVEOF'",
      "DATALAKE_DATA_DIR=/home/${var.vm_user}",
      "MINIO_ROOT_USER=${var.aistor_access_key}",
      "MINIO_ROOT_PASSWORD=${var.aistor_secret_key}",
      "JUPYTER_TOKEN=${var.jupyter_token}",
      "SUPERSET_SECRET_KEY=DataLakeSupersetSecret2025xK42",
      "SUPERSET_ADMIN_PASSWORD=${var.superset_admin_password}",
      "AIRFLOW_SECRET_KEY=AirflowDatalakeSecret2025xK42",
      "AIRFLOW_PASSWORD=${var.airflow_password}",
      "TRUENAS_HOST=${var.truenas_script_host}",
      "TRUENAS_API_KEY=${var.truenas_api_key}",
      "JELLYFIN_API_KEY=${var.jellyfin_api_key}",
      "ENVEOF",
      "chmod 600 $HOME/.datalake.env",
      # Sync datalake .env for host-level manual script runs
      "cp $HOME/.datalake.env $HOME/datalake/.env 2>/dev/null || true",
    ]
  }

  # Step 4 — pull images and start services
  provisioner "remote-exec" {
    inline = [
      "echo '${var.vm_password}' | sudo -S docker compose --env-file $HOME/.datalake.env -f $HOME/docker-compose.yml pull --quiet",
      "echo '${var.vm_password}' | sudo -S docker compose --env-file $HOME/.datalake.env -f $HOME/docker-compose.yml up -d --remove-orphans",
      "sleep 30",
      "echo '${var.vm_password}' | sudo -S docker compose -f $HOME/docker-compose.yml ps",
    ]
  }

  provisioner "remote-exec" {
    when       = destroy
    on_failure = continue
    connection {
      type     = "ssh"
      host     = self.triggers.vm_host
      user     = self.triggers.vm_user
      password = self.triggers.vm_password
    }
    inline = [
      "sudo docker compose -f $HOME/docker-compose.yml down --remove-orphans 2>/dev/null || true",
    ]
  }
}
