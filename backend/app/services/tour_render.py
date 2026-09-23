from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class TourRenderReadinessError(ValueError):
    pass


def file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_tour_snapshot(cursor, project_id: Any) -> dict[str, Any]:
    cursor.execute(
        "SELECT p.project_type,pp.id,pp.version,pp.status,pp.materialized_at "
        "FROM projects p LEFT JOIN project_plans pp ON pp.project_id=p.id "
        "AND pp.status='APPROVED' WHERE p.id=%s ORDER BY pp.version DESC LIMIT 1",
        (project_id,),
    )
    project = cursor.fetchone()
    if project is None:
        raise TourRenderReadinessError("project not found")
    if project[0] != "TOUR_GUIDE":
        raise TourRenderReadinessError("tour render is available only for tour guide projects")
    if not project[1] or project[3] != "APPROVED" or project[4] is None:
        raise TourRenderReadinessError("an approved materialized tour plan is required")
    cursor.execute(
        "SELECT s.id,s.ordinal,s.source_plan_id,s.source_plan_version,s.source_plan_item_key,"
        "s.source_scene_duration,s.selected_variant_id,v.storage_key,a.id,a.storage_key,"
        "a.checksum,a.narration_checksum,a.media_duration,s.source_narration,a.source_plan_id,"
        "a.source_plan_version,a.source_plan_item_key,ga.parameters,v.duration FROM shots s "
        "LEFT JOIN shot_variants v ON v.id=s.selected_variant_id AND v.shot_id=s.id "
        "LEFT JOIN generation_attempts ga ON ga.id=v.generation_attempt_id "
        "LEFT JOIN assets a ON a.shot_id=s.id AND a.asset_type='VOICEOVER' "
        "WHERE s.project_id=%s ORDER BY s.ordinal",
        (project_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        raise TourRenderReadinessError("tour plan has no production shots")
    shots = []
    for expected_ordinal, row in enumerate(rows, 1):
        narration_checksum = (
            hashlib.sha256(row[13].encode("utf-8")).hexdigest() if row[13] else None
        )
        if row[1] != expected_ordinal:
            raise TourRenderReadinessError("tour shot order is not contiguous")
        if row[2] != project[1] or row[3] != project[2] or not row[4] or not row[5]:
            raise TourRenderReadinessError("tour shot provenance is stale")
        if not row[6] or not row[7]:
            raise TourRenderReadinessError(f"shot {row[1]} has no selected variant")
        if not row[8] or not row[9] or not row[10]:
            raise TourRenderReadinessError(f"shot {row[1]} has no voiceover")
        if row[14] != project[1] or row[15] != project[2] or row[16] != row[4]:
            raise TourRenderReadinessError(f"shot {row[1]} voiceover provenance is stale")
        if row[11] != narration_checksum:
            raise TourRenderReadinessError(f"shot {row[1]} voiceover provenance is stale")
        variant_provenance = (row[17] or {}).get("tour_provenance", {})
        if variant_provenance != {
            "source_plan_id": str(project[1]),
            "source_plan_version": project[2],
            "source_plan_item_key": row[4],
        }:
            raise TourRenderReadinessError(f"shot {row[1]} variant provenance is stale")
        variant_parameters = row[17] or {}
        if variant_parameters.get("origin") == "producer_upload" and (
            abs(float(variant_parameters.get("target_duration", -1)) - float(row[5])) > 0.01
            or abs(float(row[18]) - float(row[5])) > 0.15
        ):
            raise TourRenderReadinessError(f"shot {row[1]} producer footage is stale")
        if float(row[12]) > float(row[5]) + 0.01:
            raise TourRenderReadinessError(f"shot {row[1]} voiceover exceeds scene duration")
        shots.append(
            {
                "shot_id": str(row[0]),
                "ordinal": row[1],
                "source_plan_item_key": row[4],
                "duration": float(row[5]),
                "variant_id": str(row[6]),
                "variant_key": row[7],
                "voiceover_asset_id": str(row[8]),
                "voiceover_key": row[9],
                "voiceover_checksum": row[10],
                "narration_checksum": row[11],
                "voiceover_duration": float(row[12]),
                "variant_origin": (row[17] or {}).get("origin", "provider_generation"),
                "original_asset_id": (row[17] or {}).get("original_asset_id"),
                "original_checksum": (row[17] or {}).get("original_checksum"),
            }
        )
    return {
        "source_plan_id": str(project[1]),
        "source_plan_version": project[2],
        "shots": shots,
        "expected_duration": sum(shot["duration"] for shot in shots),
    }
