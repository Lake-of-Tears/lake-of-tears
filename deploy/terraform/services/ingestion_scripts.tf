resource "null_resource" "ingestion_scripts" {
  depends_on = [null_resource.docker_setup]

  triggers = {
    vm_host     = var.vm_host
    vm_user     = var.vm_user
    vm_password = var.vm_password
    scripts_hash = join(",", [
      filemd5("${path.module}/../../scripts/storage_writer.py"),
      filemd5("${path.module}/../../scripts/jobs/pipeline/ingest_alexa.py"),
      filemd5("${path.module}/../../scripts/jobs/pipeline/ingest_jellyfin.py"),
      filemd5("${path.module}/../../scripts/jobs/pipeline/ingest_truenas.py"),
      filemd5("${path.module}/../../scripts/jobs/pipeline/ingest_truenas_logs.py"),
      filemd5("${path.module}/../../scripts/jobs/pipeline/ingest_weather.py"),
    ])
    # Airflow DAG hashes trigger re-deploy when DAGs change
    dags_hash = join(",", [
      filemd5("${path.module}/../../scripts/jobs/dags/ingest_truenas_dag.py"),
      filemd5("${path.module}/../../scripts/jobs/dags/ingest_jellyfin_dag.py"),
      filemd5("${path.module}/../../scripts/jobs/dags/ingest_weather_dag.py"),
      filemd5("${path.module}/../../scripts/jobs/dags/ingest_truenas_logs_dag.py"),
    ])
  }

  connection {
    type     = "ssh"
    host     = self.triggers.vm_host
    user     = self.triggers.vm_user
    password = self.triggers.vm_password
  }

  provisioner "remote-exec" {
    inline = ["mkdir -p $HOME/datalake $HOME/alexa-exports $HOME/datalake/logs"]
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "tar czf - -C ${path.module}/../../scripts . | sshpass -p '${var.vm_password}' ssh -o StrictHostKeyChecking=no ${var.vm_user}@${var.vm_host} 'tar xzf - -C $HOME/datalake'"
  }

  provisioner "remote-exec" {
    inline = [
      "cat > $HOME/datalake/.env <<'ENVEOF'",
      "AISTOR_ENDPOINT=minio:9000",
      "AISTOR_KEY=${var.aistor_access_key}",
      "AISTOR_SECRET=${var.aistor_secret_key}",
      "TRUENAS_HOST=${var.truenas_script_host}",
      "TRUENAS_API_KEY=${var.truenas_api_key}",
      "JELLYFIN_URL=http://${var.truenas_script_host}:8096",
      "JELLYFIN_API_KEY=${var.jellyfin_api_key}",
      "ENVEOF",
      "chmod 600 $HOME/datalake/.env",
      "echo '${var.vm_password}' | sudo -S apt-get install -y python3-pip python3-venv -qq",
      "pip3 install duckdb requests --break-system-packages --quiet",
      "crontab -l 2>/dev/null | grep -v '# datalake-managed' | crontab -",
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
      "crontab -l 2>/dev/null | grep -v '# datalake-managed' | crontab -",
      "rm -rf $HOME/datalake",
    ]
  }
}
