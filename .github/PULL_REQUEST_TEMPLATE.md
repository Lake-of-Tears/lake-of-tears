## Summary

<!-- What does this PR do? Why? -->

## Type of change

- [ ] Bug fix
- [ ] New data source connector
- [ ] New feature
- [ ] Refactor / cleanup
- [ ] Documentation
- [ ] Infrastructure / deployment (Helm, Terraform, Docker)

## Checklist

- [ ] `ruff check .` passes locally
- [ ] `ruff format .` applied
- [ ] Docker build passes (`docker build ui/`) — if UI changed
- [ ] `helm lint deploy/helm/lake-of-tears --namespace lake-of-tears` passes — if chart changed
- [ ] `terraform validate` passes — if Terraform changed
- [ ] Documentation updated (README, CLAUDE.md) — if behavior changed
- [ ] Added source to `SOURCES` in `embed_sources.py` — if adding a new data source

## Related issues

Closes #
