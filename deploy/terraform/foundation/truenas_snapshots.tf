# Daily ZFS snapshot of the datalake dataset at 02:00, retained for
# snapshot_retention_days days. Uses midclt locally; idempotent.

resource "null_resource" "datalake_snapshots" {
  depends_on = [null_resource.datalake_dataset]

  triggers = {
    dataset          = "${var.zfs_pool}/datalake"
    retention        = var.snapshot_retention_days
    truenas_host     = var.truenas_host
    truenas_password = var.truenas_password
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-SSHCMD
      sshpass -p '${var.truenas_password}' \
        ssh -o StrictHostKeyChecking=no root@${var.truenas_host} python3 - <<'PYEOF'
      import json, subprocess, sys

      dataset   = "${var.zfs_pool}/datalake"
      retention = ${var.snapshot_retention_days}
      schema    = "datalake-auto-%Y-%m-%d_%H-%M"

      def midclt(method, *args):
          r = subprocess.run(["midclt", "call", method] + list(args),
                             capture_output=True, text=True, timeout=60)
          if r.returncode != 0:
              print("midclt error:", r.stderr, file=sys.stderr)
              sys.exit(1)
          return json.loads(r.stdout.strip())

      existing = midclt("pool.snapshottask.query",
                        f'[["dataset","=","{dataset}"],["naming_schema","=","{schema}"]]')
      if existing:
          print("Snapshot task already exists, skipping")
          sys.exit(0)

      payload = {
          "dataset":        dataset,
          "recursive":      True,
          "lifetime_value": retention,
          "lifetime_unit":  "DAY",
          "enabled":        True,
          "naming_schema":  schema,
          "schedule":       {"minute": "0", "hour": "2", "dom": "*", "month": "*", "dow": "*"},
      }
      midclt("pool.snapshottask.create", json.dumps(payload))
      print(f"Snapshot task created: daily at 02:00, retain {retention} days")
      PYEOF
    SSHCMD
  }

  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
    interpreter = ["/bin/bash", "-c"]
    command     = <<-SSHCMD
      sshpass -p '${self.triggers.truenas_password}' \
        ssh -o StrictHostKeyChecking=no root@${self.triggers.truenas_host} python3 - <<'PYEOF' || true
      import json, subprocess, sys

      dataset = "${self.triggers.dataset}"
      schema  = "datalake-auto-%Y-%m-%d_%H-%M"

      def midclt(method, *args):
          r = subprocess.run(["midclt", "call", method] + list(args),
                             capture_output=True, text=True, timeout=60)
          return json.loads(r.stdout.strip()) if r.returncode == 0 else None

      tasks = midclt("pool.snapshottask.query",
                     f'[["dataset","=","{dataset}"],["naming_schema","=","{schema}"]]')
      if tasks:
          task_id = tasks[0]["id"]
          midclt("pool.snapshottask.delete", str(task_id))
          print(f"Snapshot task {task_id} removed")
      PYEOF
    SSHCMD
  }
}
