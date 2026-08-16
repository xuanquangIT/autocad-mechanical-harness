"""Production golden evidence must be real, hash-locked, and independent."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, cast

import pytest
import scripts.check_production_golden_acceptance as verifier
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from scripts.check_production_golden_acceptance import main, verify_production_golden_acceptance

from cad_harness.adapters.dxf_drawing_reader import DxfDrawingReader
from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.domain.canonical import sha256_of
from cad_harness.domain.models.base import SCHEMA_VERSION
from cad_harness.domain.models.drawing_model import DrawingModel, ReadScope
from cad_harness.domain.models.takeoff import TakeoffReport
from cad_harness.domain.ports.drawing_source import DrawingReadRequest, DrawingSourceRef
from cad_harness.security.evidence_attestation import (
    EvidenceRole,
    TrustPolicyIdentity,
    issue_attestation,
    trust_policy_from_mapping,
    trust_policy_sha256,
)

_ISSUED_AT = datetime(2026, 8, 15, tzinfo=UTC)


def _keypair(marker: int) -> tuple[str, str]:
    key = Ed25519PrivateKey.from_private_bytes(bytes([marker]) * 32)
    private = (
        base64.urlsafe_b64encode(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    public = (
        base64.urlsafe_b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    return private, public


_ROLE_CONFIG = {
    role: (identity_id, *_keypair(index))
    for index, (role, identity_id) in enumerate(
        (
            (EvidenceRole.ENGINEER_SELECTOR, "selector-authority"),
            (EvidenceRole.GOLDEN_REVIEWER, "golden-review-authority"),
            (EvidenceRole.COMPANY_PROFILE_APPROVER, "profile-approval-authority"),
            (EvidenceRole.TAKEOFF_CALCULATOR, "takeoff-calculation-authority"),
            (EvidenceRole.TAKEOFF_REVIEWER, "takeoff-review-authority"),
            (EvidenceRole.MATERIAL_TABLE_APPROVER, "material-approval-authority"),
        ),
        start=1,
    )
}
_IDENTITIES = {
    role: TrustPolicyIdentity(
        identity_id=identity_id,
        allowed_roles=(role,),
        public_key=public_key,
    )
    for role, (identity_id, _, public_key) in _ROLE_CONFIG.items()
}
_TRUST_CONTEXT: dict[Path, tuple[Path, str]] = {}


def _attestation(role: EvidenceRole, claims: dict[str, Any]) -> dict[str, str | None]:
    return issue_attestation(
        claims,
        _IDENTITIES[role],
        role,
        _ROLE_CONFIG[role][1],
        issued_at=_ISSUED_AT,
    ).to_external_dict()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pointer(root: Path, path: Path) -> dict[str, str]:
    return {"artifact_ref": path.relative_to(root).as_posix(), "sha256": _sha256(path)}


def _write_json(root: Path, path: Path, payload: object) -> dict[str, str]:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return _pointer(root, path)


def _write_yaml(root: Path, path: Path, payload: object) -> dict[str, str]:
    path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    return _pointer(root, path)


def _source_dxf(index: int) -> bytes:
    width = 100 + index
    return (
        "0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1024\n"
        "9\n$INSUNITS\n70\n4\n0\nENDSEC\n"
        "0\nSECTION\n2\nTABLES\n0\nTABLE\n2\nLAYER\n70\n1\n"
        "0\nLAYER\n2\nOBJECT\n70\n0\n62\n7\n6\nCONTINUOUS\n"
        "0\nENDTAB\n0\nENDSEC\n"
        "0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n5\n10\n"
        "100\nAcDbEntity\n8\nOBJECT\n100\nAcDbPolyline\n90\n4\n70\n1\n"
        f"10\n0\n20\n0\n10\n{width}\n20\n0\n10\n{width}\n20\n50\n"
        "10\n0\n20\n50\n"
        "0\nENDSEC\n0\nEOF\n"
    ).encode()


def _profile_payload() -> dict[str, object]:
    return {
        "profile_id": "production-profile",
        "version": "2.0",
        "company_approved": True,
        "canonical_unit": "mm",
        "general_tolerance": "ISO 2768-m",
        "layers": [
            {
                "name": "OBJECT",
                "purpose": "Visible fabrication geometry",
                "color_index": 7,
                "linetype": "Continuous",
            }
        ],
        "layer_map": {"outline": "OBJECT"},
        "material_profile_ref": "production-materials@2.0",
    }


def _mass_text(value: Decimal) -> str:
    rendered = format(value, "f")
    whole, separator, fractional = rendered.partition(".")
    return f"{whole}.{fractional.ljust(6, '0')}" if separator else f"{whole}.000000"


def _takeoff_report(index: int, source_sha256: str) -> dict[str, object]:
    width = 100 + index
    area = Decimal(width * 50)
    raw_mass = area * Decimal("10.0") * Decimal("7850.0") / Decimal("1000000000")
    total_raw = raw_mass * 2
    entity_ref = "10"
    evidence = {
        field: [entity_ref]
        for field in (
            "density_kg_per_m3",
            "thickness_mm",
            "quantity",
            "net_area_mm2",
            "gross_area_mm2",
            "unit_mass_kg",
            "unit_mass_kg_raw",
            "unit_mass_kg_raw_text",
            "total_mass_kg",
            "total_mass_kg_raw",
            "total_mass_kg_raw_text",
            "cut_length_mm",
            "outer_cut_length_mm",
            "inner_cut_length_mm",
            "pierce_count",
            "hole_groups",
            "weld_length_mm",
        )
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "document_id": f"document-{index:02d}",
        "revision": f"sha256:{source_sha256}",
        "profile_id": "production-materials",
        "material_profile_id": "production-materials",
        "material_profile_version": "2.0",
        "company_approved": True,
        "parts": [
            {
                "part_code": f"PLATE-{index:02d}",
                "material_code": "S355",
                "density_kg_per_m3": 7850.0,
                "thickness_mm": 10.0,
                "quantity": 2,
                "net_area_mm2": float(area),
                "gross_area_mm2": None,
                "unit_mass_kg": float(raw_mass.quantize(Decimal("0.001"), ROUND_HALF_UP)),
                "unit_mass_kg_raw": float(raw_mass),
                "unit_mass_kg_raw_text": _mass_text(raw_mass),
                "total_mass_kg": float(total_raw.quantize(Decimal("0.001"), ROUND_HALF_UP)),
                "total_mass_kg_raw": float(total_raw),
                "total_mass_kg_raw_text": _mass_text(total_raw),
                "cut_length_mm": float(2 * (width + 50)),
                "outer_cut_length_mm": float(2 * (width + 50)),
                "inner_cut_length_mm": 0.0,
                "pierce_count": 1,
                "hole_groups": [],
                "weld_length_mm": 0.0,
                "evidence": evidence,
            }
        ],
        "excluded_contours": [],
        "units": {
            "density_kg_per_m3": "kg/m3",
            "thickness_mm": "mm",
            "quantity": "count",
            "net_area_mm2": "mm2",
            "gross_area_mm2": "mm2",
            "unit_mass_kg": "kg",
            "unit_mass_kg_raw": "kg",
            "unit_mass_kg_raw_text": "kg",
            "total_mass_kg": "kg",
            "total_mass_kg_raw": "kg",
            "total_mass_kg_raw_text": "kg",
            "cut_length_mm": "mm",
            "outer_cut_length_mm": "mm",
            "inner_cut_length_mm": "mm",
            "pierce_count": "count",
            "hole_groups.diameter_mm": "mm",
            "hole_groups.count": "count",
            "hole_groups": "diameter:mm,count:count",
            "weld_length_mm": "mm",
        },
    }


def _source_snapshot(path: Path, source_sha256: str) -> dict[str, Any]:
    profile = CompanyProfile.model_validate(_profile_payload())
    model = DxfDrawingReader(profile.tolerance()).read(
        DrawingReadRequest(
            source=DrawingSourceRef(kind="file", format="dxf", ref=str(path)),
            scope=ReadScope(kind="model_space"),
            max_entities=20_000,
            max_block_nesting_depth=10,
        )
    )
    return {
        "schema_version": "1.0",
        "source_sha256": source_sha256,
        "revision": model.revision,
        "source_unit_code": model.source_unit_code,
        "to_mm_factor": model.to_mm_factor,
        "geometry_normalized": model.geometry_normalized,
        "scope": model.scope.model_dump(mode="json", exclude_none=True),
        "entities": [
            entity.model_dump(mode="json", exclude_none=True) for entity in model.entities
        ],
        "layers": [layer.model_dump(mode="json", exclude_none=True) for layer in model.layers],
        "unsupported": [
            item.model_dump(mode="json", exclude_none=True) for item in model.unsupported
        ],
        "coverage_complete": model.coverage_complete,
    }


def _model_projection(model: DrawingModel, source_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "source_sha256": source_sha256,
        "revision": model.revision,
        "source_unit_code": model.source_unit_code,
        "to_mm_factor": model.to_mm_factor,
        "geometry_normalized": model.geometry_normalized,
        "scope": model.scope.model_dump(mode="json", exclude_none=True),
        "entities": [
            entity.model_dump(mode="json", exclude_none=True) for entity in model.entities
        ],
        "layers": [layer.model_dump(mode="json", exclude_none=True) for layer in model.layers],
        "unsupported": [
            item.model_dump(mode="json", exclude_none=True) for item in model.unsupported
        ],
        "coverage_complete": model.coverage_complete,
    }


def _dwg_model_payload(index: int) -> dict[str, Any]:
    width = float(100 + index)
    return {
        "schema_version": SCHEMA_VERSION,
        "document_id": f"dwg-document-{index:02d}",
        "revision": f"bridge-revision-{index:02d}",
        "display_name": "controlled-production-source.dwg",
        "source_unit_code": "mm",
        "to_mm_factor": 1.0,
        "geometry_normalized": True,
        "scope": {"kind": "model_space", "entity_refs": []},
        "entities": [
            {
                "entity_ref": "A1",
                "entity_type": "AcDbPolyline",
                "layer": "OBJECT",
                "visible": True,
                "space": "model",
                "geometry": {
                    "kind": "polyline",
                    "vertices": [
                        {"point_mm": [0.0, 0.0], "bulge": 0.0},
                        {"point_mm": [width, 0.0], "bulge": 0.0},
                        {"point_mm": [width, 50.0], "bulge": 0.0},
                        {"point_mm": [0.0, 50.0], "bulge": 0.0},
                    ],
                    "closed": True,
                },
                "bounding_box_mm": [0.0, 0.0, width, 50.0],
                "non_uniform_scale": False,
                "feature_id": f"plate-{index:02d}",
            }
        ],
        "layers": [],
        "dimension_styles": [],
        "text_styles": [],
        "unsupported": [],
        "coverage_complete": True,
        "arc_chord_tolerance_mm": 0.01,
    }


def _write_case(root: Path, index: int, *, takeoff: bool) -> dict[str, object]:
    case_id = f"golden-{index:02d}"
    case_dir = root / f"bundle-{index:02d}"
    case_dir.mkdir()
    source_path = case_dir / "source.dxf"
    source_path.write_bytes(_source_dxf(index))
    source_sha256 = _sha256(source_path)
    provenance_ref = f"controlled-source-{index:02d}"
    provenance_pointer = _write_json(
        root,
        case_dir / "source_provenance.json",
        {
            "schema_version": "1.0",
            "evidence_kind": "production_source_provenance",
            "source_sha256": source_sha256,
            "source_class": "customer_local_reviewed",
            "synthetic": False,
            "development_fixture": False,
            "provenance_type": "customer",
            "provenance": {
                "customer_record_ref": provenance_ref,
                "custodian_ref": f"controlled-custodian-{index:02d}",
            },
        },
    )
    source_pointer: dict[str, object] = {
        **_pointer(root, source_path),
        "provenance_ref": provenance_ref,
        "provenance": provenance_pointer,
        "source_class": "customer_local_reviewed",
        "synthetic": False,
        "development_fixture": False,
    }

    artifact_payloads = {
        "input_spec": (
            "input_spec.json",
            {
                "schema_version": SCHEMA_VERSION,
                "spec_id": f"spec-{index:02d}",
                "document_id": f"document-{index:02d}",
                "units": "mm",
                "standard_profile": {"profile_id": "production-profile", "version": "2.0"},
                "drawing": {
                    "projection": "orthographic",
                    "view": "top",
                    "datum": {"type": "point", "point_mm": [0.0, 0.0]},
                },
                "features": [
                    {
                        "feature_id": f"plate-{index:02d}",
                        "type": "rectangular_plate",
                        "parameters": {
                            "width_mm": float(100 + index),
                            "height_mm": 50.0,
                            "thickness_mm": 10.0,
                            "material": "S355",
                            "origin_mm": [0.0, 0.0],
                        },
                    }
                ],
                "annotations": {"dimensions": "none"},
            },
        ),
        "expected_plan": (
            "expected_plan.json",
            {
                "canonical_units": "mm",
                "profile_ref": "production-profile@2.0",
                "operations": [
                    {
                        "operation_id": f"op:plate-{index:02d}:outline",
                        "feature_id": f"plate-{index:02d}",
                        "type": "create_closed_polyline",
                        "layer": "OBJECT",
                        "geometry": {
                            "vertices_mm": [
                                [0.0, 0.0],
                                [float(100 + index), 0.0],
                                [float(100 + index), 50.0],
                                [0.0, 50.0],
                            ]
                        },
                        "expected": {
                            "closed": True,
                            "vertex_count": 4,
                            "width_mm": float(100 + index),
                            "height_mm": 50.0,
                            "area_mm2": float((100 + index) * 50),
                        },
                    }
                ],
            },
        ),
        "expected_semantic_entities": (
            "expected_semantic_entities.json",
            {
                "entity_count": 1,
                "entities": [
                    {
                        "operation_id": f"op:plate-{index:02d}:outline",
                        "feature_id": f"plate-{index:02d}",
                        "entity_type": "AcDbPolyline",
                        "layer": "OBJECT",
                        "measurements": {
                            "closed": True,
                            "vertex_count": 4,
                            "width_mm": float(100 + index),
                            "height_mm": 50.0,
                            "area_mm2": float((100 + index) * 50),
                            "perimeter_mm": float(2 * (100 + index + 50)),
                        },
                    }
                ],
            },
        ),
        "expected_validation": (
            "expected_validation.json",
            {
                "stage": "pre_commit",
                "blocking_count": 0,
                "error_count": 0,
                "warning_count": 0,
                "commit_allowed": True,
                "findings": [],
            },
        ),
    }
    artifacts: dict[str, dict[str, str]] = {}
    for field, (filename, payload) in artifact_payloads.items():
        artifacts[field] = _write_json(root, case_dir / filename, payload)
    artifacts["company_profile"] = _write_yaml(
        root, case_dir / "company_profile.yaml", _profile_payload()
    )
    preview_path = case_dir / "preview_reference.svg"
    preview_path.write_text(
        "<svg xmlns='http://www.w3.org/2000/svg' width='120' height='70'>"
        "<rect x='10' y='10' width='100' height='50'/></svg>",
        encoding="utf-8",
    )
    artifacts["preview_reference"] = _pointer(root, preview_path)
    source_pointer["semantic_snapshot"] = _write_json(
        root,
        case_dir / "source_semantic_snapshot.json",
        _source_snapshot(source_path, source_sha256),
    )

    result: dict[str, object] = {
        "case_id": case_id,
        "case_type": "takeoff" if takeoff else "design",
        "production_evidence": True,
        "review_status": "approved",
        "engineer_selected": True,
        "selector_identity": _IDENTITIES[EvidenceRole.ENGINEER_SELECTOR].identity_id,
        "artifacts": artifacts,
        "source_drawing": source_pointer,
        "company_profile_attestation": _attestation(
            EvidenceRole.COMPANY_PROFILE_APPROVER,
            {
                "evidence_kind": "company_profile_approval_attestation",
                "company_profile_ref": "production-profile@2.0",
                "company_profile_sha256": artifacts["company_profile"]["sha256"],
                "approved": True,
            },
        ),
    }
    if takeoff:
        artifacts["input_drawing"] = {
            "artifact_ref": str(source_pointer["artifact_ref"]),
            "sha256": str(source_pointer["sha256"]),
        }
        expected_takeoff = case_dir / "expected_takeoff.json"
        expected_takeoff_payload = _takeoff_report(index, str(source_pointer["sha256"]))
        artifacts["expected_takeoff"] = _write_json(
            root,
            expected_takeoff,
            expected_takeoff_payload,
        )
        artifacts["takeoff_request"] = _write_json(
            root,
            case_dir / "takeoff_request.json",
            {
                "schema_version": SCHEMA_VERSION,
                "document_id": f"document-{index:02d}",
                "parts": [
                    {
                        "part_code": f"PLATE-{index:02d}",
                        "outline_entity_ref": "10",
                        "thickness_mm": 10.0,
                        "material_code": "S355",
                        "quantity": 2,
                        "inner_contour_entity_refs": [],
                    }
                ],
                "weld_edges": [],
                "material_profile_ref": "production-materials@2.0",
            },
        )
        table_path = case_dir / "material_table.yaml"
        table_pointer = _write_yaml(
            root,
            table_path,
            {
                "profile_id": "production-materials",
                "version": "2.0",
                "company_approved": True,
                "entries": [
                    {
                        "material_code": "S355",
                        "description": "Structural steel plate",
                        "density_kg_per_m3": 7850.0,
                    }
                ],
            },
        )
        approval_ref = "company-material-approval-2026"
        approval_path = case_dir / "material_approval.json"
        approval_pointer = _write_json(
            root,
            approval_path,
            {
                "evidence_kind": "company_material_table_approval",
                "evidence_ref": approval_ref,
                "material_profile_ref": "production-materials@2.0",
                "material_table_sha256": table_pointer["sha256"],
                "approved": True,
                "approved_by": "company-standards-owner",
            },
        )
        calculation_ref = f"independent-calculation-{index:02d}"
        calculation_path = case_dir / "calculation.json"
        calculation_pointer = _write_json(
            root,
            calculation_path,
            {
                "evidence_kind": "independent_takeoff_calculation",
                "case_id": case_id,
                "evidence_ref": calculation_ref,
                "calculated_by": _IDENTITIES[EvidenceRole.TAKEOFF_CALCULATOR].identity_id,
                "source_sha256": source_pointer["sha256"],
                "expected_takeoff_sha256": artifacts["expected_takeoff"]["sha256"],
                "method": "independent analytic contour calculation",
            },
        )
        takeoff_evidence: dict[str, Any] = {
            "calculated_by": _IDENTITIES[EvidenceRole.TAKEOFF_CALCULATOR].identity_id,
            "reviewer_identity": _IDENTITIES[EvidenceRole.TAKEOFF_REVIEWER].identity_id,
            "calculation_source": {"evidence_ref": calculation_ref, **calculation_pointer},
            "material_table": {
                "ref": "production-materials@2.0",
                "company_approved": True,
                "table": table_pointer,
                "approval": {"evidence_ref": approval_ref, **approval_pointer},
            },
        }
        result["takeoff"] = takeoff_evidence
        recomputed_hash = sha256_of(
            TakeoffReport.model_validate(expected_takeoff_payload).model_dump(
                mode="json", exclude_none=True
            )
        )
        calculation_claims = {
            "evidence_kind": "takeoff_calculation_attestation",
            "case_id": case_id,
            "source_sha256": source_pointer["sha256"],
            "takeoff_request_sha256": artifacts["takeoff_request"]["sha256"],
            "expected_takeoff_sha256": artifacts["expected_takeoff"]["sha256"],
            "recomputed_takeoff_sha256": recomputed_hash,
            "calculation_evidence_sha256": calculation_pointer["sha256"],
        }
        takeoff_evidence["calculator_attestation"] = _attestation(
            EvidenceRole.TAKEOFF_CALCULATOR, calculation_claims
        )
        takeoff_evidence["reviewer_attestation"] = _attestation(
            EvidenceRole.TAKEOFF_REVIEWER,
            {
                **calculation_claims,
                "evidence_kind": "takeoff_review_attestation",
                "calculator_identity": _IDENTITIES[EvidenceRole.TAKEOFF_CALCULATOR].identity_id,
                "accepted": True,
            },
        )
        cast(dict[str, Any], takeoff_evidence["material_table"])["attestation"] = _attestation(
            EvidenceRole.MATERIAL_TABLE_APPROVER,
            {
                "evidence_kind": "material_table_approval_attestation",
                "case_id": case_id,
                "material_profile_ref": "production-materials@2.0",
                "material_table_sha256": table_pointer["sha256"],
                "approval_evidence_sha256": approval_pointer["sha256"],
                "approved": True,
            },
        )

    reviewer = _IDENTITIES[EvidenceRole.GOLDEN_REVIEWER].identity_id
    evidence_ref = f"golden-review-record-{index:02d}"
    review_path = case_dir / "review.json"
    review_pointer = _write_json(
        root,
        review_path,
        {
            "evidence_kind": "golden_case_review",
            "case_id": case_id,
            "reviewer_identity": reviewer,
            "evidence_ref": evidence_ref,
            "accepted": True,
            "source_sha256": source_pointer["sha256"],
            "artifact_sha256": {field: pointer["sha256"] for field, pointer in artifacts.items()},
            "review_scope": ["geometry", "measurements", "layers", "styles", "tolerance"],
        },
    )
    result["review"] = {
        "reviewer_identity": reviewer,
        "evidence_ref": evidence_ref,
        **review_pointer,
        "attestation": _attestation(
            EvidenceRole.GOLDEN_REVIEWER,
            {
                "evidence_kind": "production_golden_review_attestation",
                "manifest_kind": verifier.MANIFEST_KIND,
                "case_id": case_id,
                **_source_claim_fields(source_pointer),
                "artifact_sha256": {
                    field: pointer["sha256"] for field, pointer in artifacts.items()
                },
                "review_evidence_sha256": review_pointer["sha256"],
                "accepted": True,
            },
        ),
    }
    result["selector_attestation"] = _attestation(
        EvidenceRole.ENGINEER_SELECTOR,
        {
            "evidence_kind": "production_golden_selection_attestation",
            "manifest_kind": verifier.MANIFEST_KIND,
            "case_id": case_id,
            **_source_claim_fields(source_pointer),
            "artifact_sha256": {field: pointer["sha256"] for field, pointer in artifacts.items()},
            "selected": True,
        },
    )
    return result


def _manifest(tmp_path: Path) -> Path:
    cases = [_write_case(tmp_path, index, takeoff=index < 5) for index in range(30)]
    policy = tmp_path / "trust_policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "policy_kind": "production_evidence_trust_policy",
                "identities": [
                    {
                        "identity_id": identity.identity_id,
                        "allowed_roles": [role.value for role in identity.allowed_roles],
                        "public_key": identity.public_key,
                    }
                    for identity in _IDENTITIES.values()
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    path = tmp_path / "production_manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "manifest_kind": verifier.MANIFEST_KIND,
                "production_evidence": True,
                "review_status": "approved",
                "cases": cases,
            }
        ),
        encoding="utf-8",
    )
    policy_model = trust_policy_from_mapping(json.loads(policy.read_text(encoding="utf-8")))
    _TRUST_CONTEXT[path.resolve()] = (policy, trust_policy_sha256(policy_model))
    return path


def _verify(
    manifest: Path,
    *,
    env: dict[str, str] | None = None,
    now: datetime | None = None,
    trust_policy_path: Path | None = None,
    policy_sha256: str | None = None,
) -> dict[str, Any]:
    context = _TRUST_CONTEXT.get(manifest.resolve())
    if context is None:
        return verify_production_golden_acceptance(
            manifest,
            trust_policy_path=trust_policy_path,
            trust_policy_sha256=policy_sha256,
            env=env,
            now=now,
        )
    policy, configured_digest = context
    return verify_production_golden_acceptance(
        manifest,
        trust_policy_path=policy if trust_policy_path is None else trust_policy_path,
        trust_policy_sha256=configured_digest if policy_sha256 is None else policy_sha256,
        env=env,
        now=now,
    )


def _payload(manifest: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(manifest.read_text(encoding="utf-8")))


def _save(manifest: Path, payload: dict[str, Any]) -> None:
    manifest.write_text(json.dumps(payload), encoding="utf-8")


def _artifact_file(root: Path, pointer: dict[str, Any]) -> Path:
    return root / str(pointer["artifact_ref"])


def _sync_review(root: Path, case: dict[str, Any]) -> None:
    review = case["review"]
    review_path = _artifact_file(root, review)
    payload = json.loads(review_path.read_text(encoding="utf-8"))
    payload["case_id"] = case["case_id"]
    payload["reviewer_identity"] = review["reviewer_identity"]
    payload["evidence_ref"] = review["evidence_ref"]
    payload["source_sha256"] = case["source_drawing"]["sha256"]
    payload["artifact_sha256"] = {
        field: pointer["sha256"] for field, pointer in case["artifacts"].items()
    }
    review_path.write_text(json.dumps(payload), encoding="utf-8")
    review["sha256"] = _sha256(review_path)


def _sync_calculation(root: Path, case: dict[str, Any]) -> None:
    calculation = case["takeoff"]["calculation_source"]
    path = _artifact_file(root, calculation)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["source_sha256"] = case["source_drawing"]["sha256"]
    payload["expected_takeoff_sha256"] = case["artifacts"]["expected_takeoff"]["sha256"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    calculation["sha256"] = _sha256(path)


def _sync_source_provenance(root: Path, case: dict[str, Any]) -> None:
    pointer = case["source_drawing"]["provenance"]
    path = _artifact_file(root, pointer)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["source_sha256"] = case["source_drawing"]["sha256"]
    payload["source_class"] = case["source_drawing"]["source_class"]
    payload["synthetic"] = case["source_drawing"]["synthetic"]
    payload["development_fixture"] = case["source_drawing"]["development_fixture"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    pointer["sha256"] = _sha256(path)


def _bind_dwg_evidence(root: Path, case: dict[str, Any], index: int) -> None:
    source = case["source_drawing"]
    case_dir = _artifact_file(root, source).parent
    model_payload = _dwg_model_payload(index)
    model_pointer = _write_json(root, case_dir / "bridge_drawing_model.json", model_payload)
    model = DrawingModel.model_validate(model_payload)
    bridge_pointer = _write_json(
        root,
        case_dir / "bridge_live_read_evidence.json",
        {
            "schema_version": "1.0",
            "evidence_kind": "dotnet_bridge_live_dwg_read",
            "source_sha256": source["sha256"],
            "drawing_model_sha256": model_pointer["sha256"],
            "document_revision": model.revision,
            "adapter_type": "dotnet_bridge",
            "cad_version": "AutoCAD Mechanical 2027",
            "coverage_complete": True,
            "semantic_projection_sha256": sha256_of(_model_projection(model, source["sha256"])),
            "expected_semantic_sha256": case["artifacts"]["expected_semantic_entities"]["sha256"],
        },
    )
    source.pop("semantic_snapshot", None)
    source["drawing_model"] = model_pointer
    source["bridge_evidence"] = bridge_pointer


def _source_claim_fields(source: dict[str, Any]) -> dict[str, Any]:
    provenance = cast(dict[str, Any], source.get("provenance"))
    semantic = cast(
        dict[str, Any], source.get("semantic_snapshot") or source.get("bridge_evidence")
    )
    drawing_model = source.get("drawing_model")
    return {
        "source_sha256": source["sha256"],
        "provenance_sha256": provenance["sha256"],
        "source_class": source["source_class"],
        "synthetic": source["synthetic"],
        "development_fixture": source["development_fixture"],
        "source_semantic_evidence_sha256": semantic["sha256"],
        "source_drawing_model_sha256": (
            drawing_model["sha256"] if isinstance(drawing_model, dict) else None
        ),
    }


def _selection_claims(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_kind": "production_golden_selection_attestation",
        "manifest_kind": verifier.MANIFEST_KIND,
        "case_id": case["case_id"],
        **_source_claim_fields(case["source_drawing"]),
        "artifact_sha256": {
            field: pointer["sha256"] for field, pointer in case["artifacts"].items()
        },
        "selected": True,
    }


def _resign_selection_and_review(case: dict[str, Any]) -> None:
    artifact_hashes = {field: pointer["sha256"] for field, pointer in case["artifacts"].items()}
    case["selector_attestation"] = _attestation(
        EvidenceRole.ENGINEER_SELECTOR,
        _selection_claims(case),
    )
    case["review"]["attestation"] = _attestation(
        EvidenceRole.GOLDEN_REVIEWER,
        {
            "evidence_kind": "production_golden_review_attestation",
            "manifest_kind": verifier.MANIFEST_KIND,
            "case_id": case["case_id"],
            **_source_claim_fields(case["source_drawing"]),
            "artifact_sha256": artifact_hashes,
            "review_evidence_sha256": case["review"]["sha256"],
            "accepted": True,
        },
    )


def test_valid_minimum_production_corpus_passes(tmp_path: Path) -> None:
    summary = _verify(_manifest(tmp_path))

    assert summary == {
        "passed": True,
        "case_count": 30,
        "takeoff_case_count": 5,
        "errors": [],
    }


def test_trust_policy_is_mandatory_but_environment_path_remains_compatible(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    policy, digest = _TRUST_CONTEXT[manifest.resolve()]

    missing = verify_production_golden_acceptance(manifest, env={})
    missing_pin = verify_production_golden_acceptance(manifest, trust_policy_path=policy, env={})
    configured = verify_production_golden_acceptance(
        manifest,
        env={
            verifier.TRUST_POLICY_ENV: str(policy),
            verifier.TRUST_POLICY_SHA256_ENV: digest,
        },
    )
    mismatched_pin = _verify(manifest, policy_sha256=f"sha256:{'0' * 64}")

    assert missing["errors"] == [{"code": "TRUST_POLICY_MISSING", "field": "trust_policy"}]
    assert missing_pin["errors"] == [
        {
            "code": "EVIDENCE_ATTESTATION_POLICY_DIGEST_MISSING",
            "field": "trust_policy_sha256",
        }
    ]
    assert any(
        error["code"] == "EVIDENCE_ATTESTATION_POLICY_DIGEST_MISMATCH"
        for error in mismatched_pin["errors"]
    )
    assert configured["passed"] is True


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        ("wrong_role", "EVIDENCE_ATTESTATION_ROLE_MISMATCH"),
        ("wrong_identity", "EVIDENCE_ATTESTATION_IDENTITY_NOT_TRUSTED"),
        ("tampered_claims_hash", "EVIDENCE_ATTESTATION_CLAIMS_MISMATCH"),
        ("tampered_signature", "EVIDENCE_ATTESTATION_SIGNATURE_INVALID"),
    ],
)
def test_attestation_trust_failures_are_rejected(
    tmp_path: Path, mutation: str, error_code: str
) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    attestation = payload["cases"][0]["selector_attestation"]
    if mutation == "wrong_role":
        attestation["role"] = EvidenceRole.GOLDEN_REVIEWER.value
    elif mutation == "wrong_identity":
        attestation["identity_id"] = "untrusted-selection-authority"
    elif mutation == "tampered_claims_hash":
        attestation["claims_sha256"] = f"sha256:{'0' * 64}"
    else:
        attestation["signature"] = "ed25519:" + base64.urlsafe_b64encode(bytes(64)).rstrip(
            b"="
        ).decode("ascii")
    _save(manifest, payload)

    summary = _verify(manifest)

    assert any(item["code"] == error_code for item in summary["errors"])


def test_selector_and_reviewer_must_be_distinct_trusted_identities(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    policy, _ = _TRUST_CONTEXT[manifest.resolve()]
    case = payload["cases"][0]
    selector = _IDENTITIES[EvidenceRole.ENGINEER_SELECTOR]
    combined = TrustPolicyIdentity(
        identity_id=selector.identity_id,
        allowed_roles=(EvidenceRole.ENGINEER_SELECTOR, EvidenceRole.GOLDEN_REVIEWER),
        public_key=selector.public_key,
    )
    policy_payload = json.loads(policy.read_text(encoding="utf-8"))
    policy_payload["identities"][0]["allowed_roles"] = [
        EvidenceRole.ENGINEER_SELECTOR.value,
        EvidenceRole.GOLDEN_REVIEWER.value,
    ]
    policy.write_text(json.dumps(policy_payload), encoding="utf-8")
    case["review"]["reviewer_identity"] = selector.identity_id
    review_claims = {
        "evidence_kind": "production_golden_review_attestation",
        "manifest_kind": verifier.MANIFEST_KIND,
        "case_id": case["case_id"],
        **_source_claim_fields(case["source_drawing"]),
        "artifact_sha256": {
            field: pointer["sha256"] for field, pointer in case["artifacts"].items()
        },
        "review_evidence_sha256": case["review"]["sha256"],
        "accepted": True,
    }
    case["review"]["attestation"] = issue_attestation(
        review_claims,
        combined,
        EvidenceRole.GOLDEN_REVIEWER,
        _ROLE_CONFIG[EvidenceRole.ENGINEER_SELECTOR][1],
        issued_at=_ISSUED_AT,
    ).to_external_dict()
    _save(manifest, payload)

    changed_policy = trust_policy_from_mapping(policy_payload)
    summary = _verify(manifest, policy_sha256=trust_policy_sha256(changed_policy))

    assert any(item["code"] == "SELECTION_NOT_INDEPENDENT" for item in summary["errors"])


def test_expired_attestation_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    case = payload["cases"][0]
    issued = datetime(2020, 1, 1, tzinfo=UTC)
    case["selector_attestation"] = issue_attestation(
        _selection_claims(case),
        _IDENTITIES[EvidenceRole.ENGINEER_SELECTOR],
        EvidenceRole.ENGINEER_SELECTOR,
        _ROLE_CONFIG[EvidenceRole.ENGINEER_SELECTOR][1],
        issued_at=issued,
        expires_at=issued + timedelta(days=1),
    ).to_external_dict()
    _save(manifest, payload)

    summary = _verify(manifest, now=datetime(2020, 1, 3, tzinfo=UTC))

    assert any(item["code"] == "EVIDENCE_ATTESTATION_EXPIRED" for item in summary["errors"])


def test_missing_manifest_fails_closed_without_logging_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "customer-secret" / "production.json"

    assert main([str(missing)]) == 1
    output = capsys.readouterr().out
    assert str(missing) not in output
    assert json.loads(output)["errors"] == [{"code": "MANIFEST_UNREADABLE", "field": "manifest"}]


def test_current_synthetic_corpus_cannot_be_claimed_as_production() -> None:
    manifest = Path("tests/golden_drawings/production_manifest.json")

    summary = _verify(manifest)

    assert summary["passed"] is False
    assert any(error["code"] == "MANIFEST_UNREADABLE" for error in summary["errors"])


def test_unapproved_company_profile_fails_production_model_gate(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    case = payload["cases"][7]
    pointer = case["artifacts"]["company_profile"]
    profile = _artifact_file(tmp_path, pointer)
    profile_payload = yaml.safe_load(profile.read_text(encoding="utf-8"))
    profile_payload["company_approved"] = False
    profile.write_text(yaml.safe_dump(profile_payload), encoding="utf-8")
    pointer["sha256"] = _sha256(profile)
    _sync_review(tmp_path, case)
    _save(manifest, payload)

    summary = _verify(manifest)

    assert {
        "code": "COMPANY_PROFILE_UNAPPROVED",
        "field": "company_profile.company_approved",
        "case_id": "case-008",
    } in summary["errors"]


def test_takeoff_calculator_cannot_review_own_result(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    payload["cases"][0]["takeoff"]["reviewer_identity"] = payload["cases"][0]["takeoff"][
        "calculated_by"
    ]
    _save(manifest, payload)

    summary = _verify(manifest)

    assert {
        "code": "TAKEOFF_NOT_INDEPENDENT",
        "field": "takeoff.reviewer_identity",
        "case_id": "case-001",
    } in summary["errors"]


def test_selector_cannot_review_selected_case(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    case = payload["cases"][6]
    case["selector_identity"] = case["review"]["reviewer_identity"].upper()
    _save(manifest, payload)

    summary = _verify(manifest)

    assert any(error["code"] == "SELECTION_NOT_INDEPENDENT" for error in summary["errors"])


def _base_hash(case: dict[str, Any]) -> None:
    case["artifacts"]["input_spec"]["sha256"] = "0" * 64


def _source_hash(case: dict[str, Any]) -> None:
    case["source_drawing"]["sha256"] = "0" * 64
    case["artifacts"]["input_drawing"]["sha256"] = "0" * 64


def _review_hash(case: dict[str, Any]) -> None:
    case["review"]["sha256"] = "0" * 64


def _calculation_hash(case: dict[str, Any]) -> None:
    case["takeoff"]["calculation_source"]["sha256"] = "0" * 64


def _material_hash(case: dict[str, Any]) -> None:
    case["takeoff"]["material_table"]["table"]["sha256"] = "0" * 64


def _material_approval_hash(case: dict[str, Any]) -> None:
    case["takeoff"]["material_table"]["approval"]["sha256"] = "0" * 64


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (_base_hash, "artifacts.input_spec.sha256"),
        (_source_hash, "source_drawing.sha256"),
        (_review_hash, "review.sha256"),
        (_calculation_hash, "takeoff.calculation_source.sha256"),
        (_material_hash, "takeoff.material_table.table.sha256"),
        (_material_approval_hash, "takeoff.material_table.approval.sha256"),
    ],
)
def test_every_evidence_class_requires_exact_sha256(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None], field: str
) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    mutate(payload["cases"][0])
    _save(manifest, payload)

    summary = _verify(manifest)

    assert any(
        error["code"] == "ARTIFACT_HASH_MISMATCH" and error["field"] == field
        for error in summary["errors"]
    )


def test_content_bearing_hash_locked_placeholder_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    case = payload["cases"][6]
    pointer = case["artifacts"]["input_spec"]
    path = _artifact_file(tmp_path, pointer)
    path.write_text('{"status":"placeholder"}', encoding="utf-8")
    pointer["sha256"] = _sha256(path)
    _sync_review(tmp_path, case)
    _save(manifest, payload)

    summary = _verify(manifest)

    assert any(
        error["code"] == "ARTIFACT_CONTENT_INVALID"
        and error["field"] == "artifacts.input_spec.artifact_ref"
        for error in summary["errors"]
    )


def test_duplicate_source_hashes_and_reused_review_evidence_are_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    first, second = payload["cases"][6:8]
    second["source_drawing"] = dict(first["source_drawing"])
    second["review"] = dict(first["review"])
    _save(manifest, payload)

    summary = _verify(manifest)

    codes = {error["code"] for error in summary["errors"]}
    assert {"SOURCE_HASH_DUPLICATE", "REVIEW_EVIDENCE_REUSED"} <= codes


def test_invalid_takeoff_contract_is_rejected_even_when_hashes_are_updated(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    case = payload["cases"][0]
    pointer = case["artifacts"]["expected_takeoff"]
    path = _artifact_file(tmp_path, pointer)
    path.write_text('{"schema_version":"1.13","parts":["not-a-report"]}', encoding="utf-8")
    pointer["sha256"] = _sha256(path)
    _sync_calculation(tmp_path, case)
    _sync_review(tmp_path, case)
    _save(manifest, payload)

    summary = _verify(manifest)

    assert any(error["code"] == "TAKEOFF_CONTRACT_INVALID" for error in summary["errors"])


def test_source_requires_dxf_or_dwg_type_and_matching_magic(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    case = payload["cases"][6]
    source = case["source_drawing"]
    original = _artifact_file(tmp_path, source)
    wrong_type = original.with_suffix(".txt")
    original.rename(wrong_type)
    source["artifact_ref"] = wrong_type.relative_to(tmp_path).as_posix()
    source["sha256"] = _sha256(wrong_type)
    _sync_review(tmp_path, case)
    _save(manifest, payload)

    summary = _verify(manifest)

    assert any(error["code"] == "ARTIFACT_TYPE_UNSUPPORTED" for error in summary["errors"])

    wrong_type.rename(original)
    original.write_bytes(b"not a real dxf drawing")
    source["artifact_ref"] = original.relative_to(tmp_path).as_posix()
    source["sha256"] = _sha256(original)
    _sync_review(tmp_path, case)
    _save(manifest, payload)
    summary = _verify(manifest)
    assert any(error["code"] == "ARTIFACT_CONTENT_INVALID" for error in summary["errors"])


def test_empty_oversized_and_reparse_artifacts_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    case = payload["cases"][6]
    pointer = case["artifacts"]["expected_plan"]
    path = _artifact_file(tmp_path, pointer)
    path.write_bytes(b"")
    pointer["sha256"] = _sha256(path)
    _save(manifest, payload)
    assert any(error["code"] == "ARTIFACT_INVALID" for error in _verify(manifest)["errors"])

    path.write_text(json.dumps({"operations": ["bounded evidence payload"]}), encoding="utf-8")
    pointer["sha256"] = _sha256(path)
    _sync_review(tmp_path, case)
    _save(manifest, payload)
    monkeypatch.setattr(verifier, "MAX_STRUCTURED_ARTIFACT_BYTES", 16)
    assert any(error["code"] == "ARTIFACT_INVALID" for error in _verify(manifest)["errors"])

    monkeypatch.undo()
    target = _artifact_file(tmp_path, case["source_drawing"])
    monkeypatch.setattr(
        verifier,
        "_is_reparse",
        lambda metadata: stat.S_ISREG(metadata.st_mode),
    )
    assert (
        verifier._ArtifactLocker(tmp_path).lock(
            target.relative_to(tmp_path).as_posix(),
            max_bytes=verifier.MAX_DRAWING_BYTES,
        )
        is None
    )


def test_manifest_and_each_case_must_be_explicitly_approved_production_evidence(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    payload["manifest_kind"] = "development_golden_corpus"
    payload["production_evidence"] = False
    payload["review_status"] = "draft"
    payload["cases"][0]["production_evidence"] = False
    payload["cases"][0]["review_status"] = "pending"
    _save(manifest, payload)

    summary = _verify(manifest)

    errors = {(item["code"], item["field"], item.get("case_id")) for item in summary["errors"]}
    assert ("MANIFEST_KIND_INVALID", "manifest_kind", None) in errors
    assert ("NOT_PRODUCTION_EVIDENCE", "production_evidence", None) in errors
    assert ("REVIEW_STATUS_INVALID", "review_status", None) in errors
    assert ("NOT_PRODUCTION_EVIDENCE", "production_evidence", "case-001") in errors
    assert ("REVIEW_STATUS_INVALID", "review_status", "case-001") in errors


@pytest.mark.parametrize(
    ("artifact_name", "junk", "error_code"),
    [
        ("input_spec", {"well_formed_but_not_a_spec": True}, "INPUT_SPEC_INVALID"),
        (
            "expected_plan",
            {"canonical_units": "mm", "profile_ref": "production-profile@2.0", "operations": []},
            "EXPECTED_PLAN_MISMATCH",
        ),
        (
            "expected_semantic_entities",
            {"entity_count": 1, "entities": [{"entity_type": "AcDbLine", "layer": "OBJECT"}]},
            "EXPECTED_SEMANTIC_MISMATCH",
        ),
        (
            "expected_validation",
            {
                "stage": "pre_commit",
                "blocking_count": 0,
                "error_count": 0,
                "warning_count": 0,
                "commit_allowed": True,
                "findings": [{"rule_id": "FICTIONAL-RULE", "severity": "warning"}],
            },
            "EXPECTED_VALIDATION_MISMATCH",
        ),
    ],
)
def test_junk_design_artifacts_cannot_pass_hash_locked_review(
    tmp_path: Path, artifact_name: str, junk: dict[str, object], error_code: str
) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    case = payload["cases"][6]
    pointer = case["artifacts"][artifact_name]
    path = _artifact_file(tmp_path, pointer)
    path.write_text(json.dumps(junk), encoding="utf-8")
    pointer["sha256"] = _sha256(path)
    _sync_review(tmp_path, case)
    _save(manifest, payload)

    summary = _verify(manifest)

    assert any(item["code"] == error_code for item in summary["errors"])


def test_nonsensical_takeoff_arithmetic_is_recomputed_and_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    case = payload["cases"][0]
    pointer = case["artifacts"]["expected_takeoff"]
    path = _artifact_file(tmp_path, pointer)
    takeoff = json.loads(path.read_text(encoding="utf-8"))
    takeoff["parts"][0]["total_mass_kg"] = 999999.0
    path.write_text(json.dumps(takeoff), encoding="utf-8")
    pointer["sha256"] = _sha256(path)
    _sync_calculation(tmp_path, case)
    _sync_review(tmp_path, case)
    _save(manifest, payload)

    summary = _verify(manifest)

    assert any(item["code"] == "EXPECTED_TAKEOFF_MISMATCH" for item in summary["errors"])


def test_provenance_substitution_without_new_signatures_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    case = payload["cases"][6]
    pointer = case["source_drawing"]["provenance"]
    path = _artifact_file(tmp_path, pointer)
    provenance = json.loads(path.read_text(encoding="utf-8"))
    provenance["provenance"]["custodian_ref"] = "substituted-custodian-record"
    path.write_text(json.dumps(provenance), encoding="utf-8")
    pointer["sha256"] = _sha256(path)
    _save(manifest, payload)

    summary = _verify(manifest)

    assert any(
        item["code"] == "EVIDENCE_ATTESTATION_CLAIMS_MISMATCH"
        and item["field"] in {"selector_attestation", "review.attestation"}
        for item in summary["errors"]
    )


def test_synthetic_and_public_development_sources_fail_even_when_resigned(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    case = payload["cases"][6]
    source = case["source_drawing"]
    source.update(
        {
            "source_class": "licensed_public_development",
            "synthetic": True,
            "development_fixture": True,
            "provenance_ref": "public-development-source",
        }
    )
    pointer = source["provenance"]
    path = _artifact_file(tmp_path, pointer)
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "evidence_kind": "production_source_provenance",
                "source_sha256": source["sha256"],
                "source_class": source["source_class"],
                "synthetic": True,
                "development_fixture": True,
                "provenance_type": "licensed",
                "provenance": {
                    "license_id": "MIT",
                    "source_ref": source["provenance_ref"],
                    "attribution_ref": "public-development-attribution",
                },
            }
        ),
        encoding="utf-8",
    )
    pointer["sha256"] = _sha256(path)
    _resign_selection_and_review(case)
    _save(manifest, payload)

    summary = _verify(manifest)

    codes = {item["code"] for item in summary["errors"]}
    assert {"SOURCE_NOT_PRODUCTION", "SOURCE_PROVENANCE_INVALID"} <= codes


def test_dxf_source_semantic_snapshot_must_match_real_reader(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    case = payload["cases"][6]
    pointer = case["source_drawing"]["semantic_snapshot"]
    path = _artifact_file(tmp_path, pointer)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot["entities"][0]["layer"] = "FABRICATED"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    pointer["sha256"] = _sha256(path)
    _resign_selection_and_review(case)
    _save(manifest, payload)

    summary = _verify(manifest)

    assert any(item["code"] == "SOURCE_SEMANTIC_MISMATCH" for item in summary["errors"])


def test_design_source_accepts_bound_dwg_but_takeoff_remains_dxf_only(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    design_case = payload["cases"][6]
    design_source = design_case["source_drawing"]
    dwg = _artifact_file(tmp_path, design_source).with_suffix(".dwg")
    dwg.write_bytes(b"AC1032" + bytes(range(64)))
    design_source["artifact_ref"] = dwg.relative_to(tmp_path).as_posix()
    design_source["sha256"] = _sha256(dwg)
    _sync_source_provenance(tmp_path, design_case)
    _sync_review(tmp_path, design_case)
    _resign_selection_and_review(design_case)
    _save(manifest, payload)
    raw_header_only = _verify(manifest)
    assert any(
        item["code"] in {"ARTIFACT_BINDING_MISSING", "DWG_BRIDGE_EVIDENCE_INVALID"}
        for item in raw_header_only["errors"]
    )

    _bind_dwg_evidence(tmp_path, design_case, 6)
    _sync_review(tmp_path, design_case)
    _resign_selection_and_review(design_case)
    _save(manifest, payload)
    assert _verify(manifest)["passed"] is True

    payload = _payload(manifest)
    takeoff_case = payload["cases"][0]
    takeoff_source = takeoff_case["source_drawing"]
    takeoff_dwg = _artifact_file(tmp_path, takeoff_source).with_suffix(".dwg")
    takeoff_dwg.write_bytes(b"AC1032" + bytes(reversed(range(64))))
    takeoff_source["artifact_ref"] = takeoff_dwg.relative_to(tmp_path).as_posix()
    takeoff_source["sha256"] = _sha256(takeoff_dwg)
    _sync_source_provenance(tmp_path, takeoff_case)
    takeoff_case["artifacts"]["input_drawing"] = {
        "artifact_ref": takeoff_source["artifact_ref"],
        "sha256": takeoff_source["sha256"],
    }
    _sync_calculation(tmp_path, takeoff_case)
    _sync_review(tmp_path, takeoff_case)
    _save(manifest, payload)
    summary = _verify(manifest)
    assert any(
        item["code"] == "ARTIFACT_TYPE_UNSUPPORTED"
        and item["field"] == "artifacts.input_drawing.artifact_ref"
        for item in summary["errors"]
    )


def test_dwg_bridge_model_semantics_must_match_expected_output(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    case = payload["cases"][6]
    source = case["source_drawing"]
    dwg = _artifact_file(tmp_path, source).with_suffix(".dwg")
    dwg.write_bytes(b"AC1032" + bytes(range(64)))
    source["artifact_ref"] = dwg.relative_to(tmp_path).as_posix()
    source["sha256"] = _sha256(dwg)
    _sync_source_provenance(tmp_path, case)
    _bind_dwg_evidence(tmp_path, case, 6)

    model_pointer = source["drawing_model"]
    model_path = _artifact_file(tmp_path, model_pointer)
    model_payload = json.loads(model_path.read_text(encoding="utf-8"))
    model_payload["entities"][0]["layer"] = "FABRICATED"
    model_path.write_text(json.dumps(model_payload), encoding="utf-8")
    model_pointer["sha256"] = _sha256(model_path)
    model = DrawingModel.model_validate(model_payload)
    bridge_pointer = source["bridge_evidence"]
    bridge_path = _artifact_file(tmp_path, bridge_pointer)
    bridge = json.loads(bridge_path.read_text(encoding="utf-8"))
    bridge["drawing_model_sha256"] = model_pointer["sha256"]
    bridge["semantic_projection_sha256"] = sha256_of(_model_projection(model, source["sha256"]))
    bridge_path.write_text(json.dumps(bridge), encoding="utf-8")
    bridge_pointer["sha256"] = _sha256(bridge_path)
    _sync_review(tmp_path, case)
    _resign_selection_and_review(case)
    _save(manifest, payload)

    summary = _verify(manifest)

    assert any(
        item["code"] == "SOURCE_SEMANTIC_MISMATCH"
        and item["field"] == "source_drawing.drawing_model"
        for item in summary["errors"]
    )


def test_stable_snapshot_detects_metadata_swap_and_locker_reads_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "evidence.json"
    artifact.write_text('{"evidence":"content-bearing"}', encoding="utf-8")
    real_signature = verifier._file_signature
    signature_calls = 0

    def drifting_signature(metadata: object) -> tuple[int, int, int, int, int]:
        nonlocal signature_calls
        signature_calls += 1
        signature = real_signature(cast(Any, metadata))
        if signature_calls == 4:
            return (*signature[:3], signature[3] + 1, signature[4])
        return signature

    monkeypatch.setattr(verifier, "_file_signature", drifting_signature)
    assert verifier._read_file_once(artifact, max_bytes=1024) is None

    monkeypatch.undo()
    real_read = verifier._read_file_once
    read_calls = 0

    def counted_read(path: Path, *, max_bytes: int) -> bytes | None:
        nonlocal read_calls
        read_calls += 1
        return real_read(path, max_bytes=max_bytes)

    monkeypatch.setattr(verifier, "_read_file_once", counted_read)
    locker = verifier._ArtifactLocker(tmp_path)
    first = locker.lock("evidence.json", max_bytes=1024)
    second = locker.lock("evidence.json", max_bytes=1024)
    assert first is second
    assert read_calls == 1


def test_summary_never_emits_paths_case_names_or_human_identities(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _manifest(tmp_path)
    payload = _payload(manifest)
    case = payload["cases"][6]
    secret_case = "C:/Customer-X/gearbox.dwg"
    secret_identity = "Jane Customer Engineer"
    secret_artifact = "Customer-X/secret-gearbox.dwg"
    case["case_id"] = secret_case
    case["selector_identity"] = secret_identity
    case["review"]["reviewer_identity"] = secret_identity
    case["source_drawing"]["artifact_ref"] = secret_artifact
    _save(manifest, payload)

    policy, digest = _TRUST_CONTEXT[manifest.resolve()]
    assert (
        main(
            [
                str(manifest),
                "--trust-policy",
                str(policy),
                "--trust-policy-sha256",
                digest,
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert str(manifest) not in output
    assert secret_case not in output
    assert secret_identity not in output
    assert secret_artifact not in output
    assert json.loads(output)["passed"] is False
