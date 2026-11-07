"""State management and drift detection service for Lake Asset Bundles."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.bundles.schemas import BundleSpec

try:
    from models import (
        Catalog,
        CatalogSchema,
        CatalogTable,
        PipelineMetadata,
        WorkspaceBundle,
        WorkspaceDashboard,
    )
except ImportError:
    from backend.models import (
        Catalog,
        CatalogSchema,
        CatalogTable,
        PipelineMetadata,
        WorkspaceBundle,
        WorkspaceDashboard,
    )


class BundleStateService:
    """Service managing WorkspaceBundle persistence, revision history, and resource drift."""

    @staticmethod
    def register_or_update_bundle(
        db: Session,
        workspace_id: uuid.UUID,
        bundle_spec: BundleSpec,
        target: str = "dev",
        deployed_by: uuid.UUID | None = None,
        git_commit_sha: str | None = None,
        resources_map: dict[str, Any] | None = None,
        status: str = "deployed",
    ) -> WorkspaceBundle:
        """Register a new bundle or update an existing deployment record for the specified target."""
        bundle_name = bundle_spec.bundle.name
        stmt = select(WorkspaceBundle).where(
            WorkspaceBundle.workspace_id == workspace_id,
            WorkspaceBundle.bundle_name == bundle_name,
            WorkspaceBundle.target == target,
        )
        bundle = db.scalar(stmt)

        manifest_data = bundle_spec.model_dump(by_alias=True, exclude_none=True)
        now = datetime.now(UTC)

        if bundle:
            bundle.version = bundle_spec.bundle.version
            if git_commit_sha is not None:
                bundle.git_commit_sha = git_commit_sha
            if deployed_by is not None:
                bundle.deployed_by = deployed_by
            bundle.status = status
            bundle.manifest_snapshot = manifest_data
            if resources_map is not None:
                bundle.resources_map = resources_map
            bundle.last_deployed_at = now
            bundle.updated_at = now
        else:
            bundle = WorkspaceBundle(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                bundle_name=bundle_name,
                target=target,
                version=bundle_spec.bundle.version,
                git_commit_sha=git_commit_sha,
                deployed_by=deployed_by,
                status=status,
                manifest_snapshot=manifest_data,
                resources_map=resources_map or {},
                created_at=now,
                updated_at=now,
                last_deployed_at=now,
            )
            db.add(bundle)

        db.flush()
        db.refresh(bundle)
        return bundle

    @staticmethod
    def get_bundle(
        db: Session,
        workspace_id: uuid.UUID,
        bundle_name: str,
        target: str = "dev",
    ) -> WorkspaceBundle | None:
        """Retrieve a specific bundle by workspace, name, and target."""
        stmt = select(WorkspaceBundle).where(
            WorkspaceBundle.workspace_id == workspace_id,
            WorkspaceBundle.bundle_name == bundle_name,
            WorkspaceBundle.target == target,
        )
        return db.scalar(stmt)

    @staticmethod
    def get_bundle_by_id(db: Session, bundle_id: uuid.UUID) -> WorkspaceBundle | None:
        """Retrieve a bundle by its primary key UUID."""
        stmt = select(WorkspaceBundle).where(WorkspaceBundle.id == bundle_id)
        return db.scalar(stmt)

    @staticmethod
    def list_bundles(
        db: Session,
        workspace_id: uuid.UUID,
        target: str | None = None,
    ) -> list[WorkspaceBundle]:
        """List all bundles deployed within a workspace, optionally filtered by target."""
        stmt = select(WorkspaceBundle).where(WorkspaceBundle.workspace_id == workspace_id)
        if target:
            stmt = stmt.where(WorkspaceBundle.target == target)
        stmt = stmt.order_by(WorkspaceBundle.updated_at.desc())
        return list(db.scalars(stmt).all())

    @staticmethod
    def update_bundle_status(
        db: Session,
        bundle_id: uuid.UUID,
        status: str,
    ) -> WorkspaceBundle:
        """Update the deployment status of a bundle."""
        bundle = BundleStateService.get_bundle_by_id(db, bundle_id)
        if not bundle:
            raise ValueError(f"Bundle with ID '{bundle_id}' not found")
        bundle.status = status
        bundle.updated_at = datetime.now(UTC)
        db.flush()
        db.refresh(bundle)
        return bundle

    @staticmethod
    def update_resources_map(
        db: Session,
        bundle_id: uuid.UUID,
        resources_map: dict[str, Any],
    ) -> WorkspaceBundle:
        """Update the deployed resource UUID inventory of a bundle."""
        bundle = BundleStateService.get_bundle_by_id(db, bundle_id)
        if not bundle:
            raise ValueError(f"Bundle with ID '{bundle_id}' not found")
        bundle.resources_map = resources_map
        bundle.updated_at = datetime.now(UTC)
        db.flush()
        db.refresh(bundle)
        return bundle

    @staticmethod
    def delete_bundle(db: Session, bundle_id: uuid.UUID) -> bool:
        """Delete a bundle record from PostgreSQL state tracking."""
        bundle = BundleStateService.get_bundle_by_id(db, bundle_id)
        if not bundle:
            return False
        db.delete(bundle)
        db.flush()
        return True

    @staticmethod
    def detect_drift(db: Session, bundle: WorkspaceBundle) -> dict[str, Any]:
        """Inspect tracked resources against live database records and identify missing assets."""
        res_map = bundle.resources_map or {}
        missing_resources: dict[str, list[str]] = {
            "catalogs": [],
            "schemas": [],
            "tables": [],
            "dashboards": [],
            "pipelines": [],
        }

        total_checked = 0
        missing_count = 0

        # Helper to parse string or UUID
        def _to_uuid(val: Any) -> uuid.UUID | None:
            if isinstance(val, uuid.UUID):
                return val
            if isinstance(val, str):
                try:
                    return uuid.UUID(val)
                except ValueError:
                    return None
            return None

        # 1. Verify Catalogs
        raw_catalogs = res_map.get("catalogs", [])
        for item in raw_catalogs:
            total_checked += 1
            cid = _to_uuid(item)
            if cid:
                cat = db.scalar(
                    select(Catalog).where(
                        Catalog.id == cid,
                        Catalog.owner_workspace_id == bundle.workspace_id,
                    )
                )
            else:
                cat = db.scalar(
                    select(Catalog).where(
                        Catalog.name == str(item),
                        Catalog.owner_workspace_id == bundle.workspace_id,
                    )
                )
            if not cat:
                missing_resources["catalogs"].append(str(item))
                missing_count += 1

        # 2. Verify Schemas
        raw_schemas = res_map.get("schemas", [])
        for item in raw_schemas:
            total_checked += 1
            sid = _to_uuid(item)
            if sid:
                sch = db.scalar(select(CatalogSchema).where(CatalogSchema.id == sid))
            else:
                sch = db.scalar(select(CatalogSchema).where(CatalogSchema.name == str(item)))
            if not sch:
                missing_resources["schemas"].append(str(item))
                missing_count += 1

        # 3. Verify Tables
        raw_tables = res_map.get("tables", [])
        for item in raw_tables:
            total_checked += 1
            tid = _to_uuid(item)
            if tid:
                tbl = db.scalar(select(CatalogTable).where(CatalogTable.id == tid))
            else:
                tbl = db.scalar(select(CatalogTable).where(CatalogTable.name == str(item)))
            if not tbl:
                missing_resources["tables"].append(str(item))
                missing_count += 1

        # 4. Verify Dashboards
        raw_dashboards = res_map.get("dashboards", [])
        for item in raw_dashboards:
            total_checked += 1
            did = _to_uuid(item)
            if did:
                dash = db.scalar(
                    select(WorkspaceDashboard).where(
                        WorkspaceDashboard.id == did,
                        WorkspaceDashboard.workspace_id == bundle.workspace_id,
                    )
                )
            else:
                dash = db.scalar(
                    select(WorkspaceDashboard).where(
                        (WorkspaceDashboard.title == str(item))
                        | (WorkspaceDashboard.slug == str(item)),
                        WorkspaceDashboard.workspace_id == bundle.workspace_id,
                    )
                )
            if not dash:
                missing_resources["dashboards"].append(str(item))
                missing_count += 1

        # 5. Verify Pipelines
        raw_pipelines = res_map.get("pipelines", [])
        for item in raw_pipelines:
            total_checked += 1
            pid = _to_uuid(item)
            if pid:
                pipe = db.scalar(
                    select(PipelineMetadata).where(
                        PipelineMetadata.id == pid,
                        PipelineMetadata.workspace_id == bundle.workspace_id,
                    )
                )
            else:
                pipe = db.scalar(
                    select(PipelineMetadata).where(
                        (PipelineMetadata.dag_id == str(item))
                        | (PipelineMetadata.display_label == str(item)),
                        PipelineMetadata.workspace_id == bundle.workspace_id,
                    )
                )
            if not pipe:
                missing_resources["pipelines"].append(str(item))
                missing_count += 1

        in_sync = missing_count == 0
        if not in_sync and bundle.status == "deployed":
            bundle.status = "pending_drift_sync"
            bundle.updated_at = datetime.now(UTC)
            db.flush()
            db.refresh(bundle)

        return {
            "bundle_id": str(bundle.id),
            "bundle_name": bundle.bundle_name,
            "target": bundle.target,
            "in_sync": in_sync,
            "total_checked": total_checked,
            "missing_count": missing_count,
            "missing_resources": missing_resources,
            "current_status": bundle.status,
            "checked_at": datetime.now(UTC).isoformat(),
        }
