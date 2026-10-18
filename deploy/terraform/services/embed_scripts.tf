resource "null_resource" "embed_scripts" {
  depends_on = [null_resource.ingestion_scripts]

  triggers = {
    vm_host     = var.vm_host
    vm_user     = var.vm_user
    vm_password = var.vm_password
    scripts_hash = join(",", [
      filemd5("${path.module}/../../scripts/gemini_embedder.py"),
      filemd5("${path.module}/../../scripts/jobs/pipeline/embed_sources.py"),
      filemd5("${path.module}/../../scripts/jobs/pipeline/rag_query.py"),
      filemd5("${path.module}/../../scripts/jobs/pipeline/anomaly_detect.py"),
      filemd5("${path.module}/../../scripts/jobs/pipeline/summarize.py"),
    ])
  }

  connection {
    type     = "ssh"
    host     = self.triggers.vm_host
    user     = self.triggers.vm_user
    password = self.triggers.vm_password
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "tar czf - -C ${path.module}/../../scripts . | sshpass -p '${var.vm_password}' ssh -o StrictHostKeyChecking=no ${var.vm_user}@${var.vm_host} 'tar xzf - -C $HOME/datalake'"
  }

  provisioner "remote-exec" {
    inline = [
      "pip3 install 'duckdb[vss]' google-genai scikit-learn --break-system-packages --quiet",
      "crontab -l 2>/dev/null | grep -v '# datalake-embed' | crontab -",
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
      "crontab -l 2>/dev/null | grep -v '# datalake-embed' | crontab -",
    ]
  }
}
