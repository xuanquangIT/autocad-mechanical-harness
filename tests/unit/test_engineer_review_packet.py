"""Engineer review packets preserve bytes while making no approval claim."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from scripts.build_engineer_review_packet import (
    EngineerReviewPacketError,
    build_engineer_review_packet,
)
from scripts.check_production_golden_acceptance import verify_production_golden_acceptance
from scripts.fetch_development_corpus import CorpusManifest, CorpusSource

from cad_harness.security.evidence_attestation import (
    trust_policy_from_mapping,
    trust_policy_sha256,
)


def _drawing_bytes(index: int) -> bytes:
    return (
        f"0\nSECTION\n2\nHEADER\n999\nopaque-test-record-{index:03d}\n0\nENDSEC\n0\nEOF\n"
    ).encode()


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path


def _public_contract() -> CorpusManifest:
    sources: list[CorpusSource] = []
    for index in range(26):
        suffix = ".dxf" if index < 24 else (".png" if index == 24 else ".pdf")
        source_id = f"licensed-development-{index:02d}"
        output = f"named-provider/original-public-name-{index:02d}{suffix}"
        metadata = {
            "source_id": source_id,
            "url": f"https://raw.githubusercontent.com/example/revision/source-{index:02d}{suffix}",
            "source_index_url": "https://github.com/example/revision",
            "license_id": "MIT",
            "license_url": "https://github.com/example/revision/LICENSE",
            "license_notice": "Development fixture; retain the MIT notice.",
            "attribution": "Public development fixture authors",
            "output": output,
            "max_bytes": 1024 * 1024,
            "intended_use": "Offline source-trust unit test.",
        }
        sources.append(
            CorpusSource(
                source_id=source_id,
                url=str(metadata["url"]),
                output=output,
                max_bytes=1024 * 1024,
                expected_sha256=None,
                metadata=metadata,
            )
        )
    return CorpusManifest(
        manifest_sha256="b" * 64,
        metadata={
            "schema_version": "1.0",
            "corpus_id": "unit-public-development",
            "purpose": "Public development fixtures only.",
            "customer_inputs_allowed": False,
            "production_evidence": False,
        },
        sources=tuple(sources),
    )


@pytest.fixture(autouse=True)
def _pin_public_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    contract = _public_contract()
    monkeypatch.setattr("scripts.build_engineer_review_packet.load_manifest", lambda _: contract)


def _local_case(content: bytes, *, source_kind: str = "customer_local") -> dict[str, Any]:
    return {
        "case_id": "intake-position-is-not-authoritative",
        "source_kind": source_kind,
        "sha256": _digest(content),
        "size_bytes": len(content),
        "format": {
            "declared_format": "dxf",
            "detected_format": "dxf",
            "header_status": "recognized",
            "signature": "ASCII_DXF",
        },
        "semantic_summary": {"status": "parsed"},
    }


def _fixture(
    root: Path,
    *,
    local_count: int = 6,
    add_excluded: bool = True,
) -> tuple[Path, Path, Path, Path, dict[Path, str]]:
    local_root = root / "customer-secret-alpha"
    public_root = root / "download-original-names"
    local_root.mkdir(parents=True)
    public_root.mkdir(parents=True)
    original_hashes: dict[Path, str] = {}
    local_cases: list[dict[str, Any]] = []
    for index in range(local_count):
        content = _drawing_bytes(index)
        path = local_root / f"confidential-part-name-{index}.dxf"
        path.write_bytes(content)
        original_hashes[path] = _digest(content)
        local_cases.append(_local_case(content))
    if add_excluded:
        generated = _drawing_bytes(700)
        generated_path = local_root / "generated-must-not-enter.dxf"
        generated_path.write_bytes(generated)
        original_hashes[generated_path] = _digest(generated)
        local_cases.append(_local_case(generated, source_kind="generated"))
        image = b"\x89PNG\r\n\x1a\nnot-a-drawing"
        (local_root / "shop-image-secret.png").write_bytes(image)
        local_cases.append(
            {
                "case_id": "image",
                "source_kind": "customer_local",
                "sha256": _digest(image),
                "size_bytes": len(image),
                "format": {
                    "declared_format": "png",
                    "detected_format": "png",
                    "header_status": "recognized",
                    "signature": "PNG",
                },
                "semantic_summary": {"status": "not_applicable"},
            }
        )
    local_manifest = _write_json(
        root / "private-intake-name.json",
        {
            "schema_version": "1.0",
            "manifest_kind": "development_corpus_intake",
            "production_claim_eligible": False,
            "case_count": len(local_cases),
            "cases": local_cases,
        },
    )

    source_contract = _public_contract()
    public_entries: list[dict[str, Any]] = []
    for index, contracted_source in enumerate(source_contract.sources, start=100):
        suffix = Path(contracted_source.output).suffix.casefold()
        if suffix == ".dxf":
            content = _drawing_bytes(index)
        elif suffix == ".png":
            content = b"\x89PNG\r\n\x1a\npublic-development-image"
        elif suffix == ".pdf":
            content = b"%PDF-1.4\npublic-development-document\n%%EOF\n"
        else:  # pragma: no cover - load_manifest rejects unsupported suffixes
            raise AssertionError("unexpected public source type")
        path = public_root.joinpath(*Path(contracted_source.output).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        original_hashes[path] = _digest(content)
        public_entries.append(
            {
                "source": contracted_source.metadata,
                "sha256": _digest(content),
                "size_bytes": len(content),
            }
        )
    public_lock = _write_json(
        root / "public-fetch-name.lock.json",
        {
            "schema_version": "1.0",
            "manifest_sha256": source_contract.manifest_sha256,
            "manifest": source_contract.metadata,
            "source_count": len(public_entries),
            "sources": public_entries,
        },
    )
    return local_manifest, local_root, public_lock, public_root, original_hashes


def _build(root: Path, output_name: str = "packet") -> tuple[dict[str, Any], Path]:
    local_manifest, local_root, public_lock, public_root, _ = _fixture(root)
    allowed = root / "data" / "review-packets"
    allowed.mkdir(parents=True)
    manifest = build_engineer_review_packet(
        local_manifest_path=local_manifest,
        local_source_root=local_root,
        public_lock_path=public_lock,
        public_source_root=public_root,
        output_root=Path(output_name),
        allowed_output_parent=allowed,
    )
    return manifest, allowed / output_name


def test_packet_is_deterministic_private_and_approval_neutral_across_roots(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first-machine-root"
    second_root = tmp_path / "second-machine-root"
    first_inputs = _fixture(first_root)
    second_inputs = _fixture(second_root)
    first_allowed = first_root / "data" / "review-packets"
    second_allowed = second_root / "data" / "review-packets"
    first_allowed.mkdir(parents=True)
    second_allowed.mkdir(parents=True)

    first = build_engineer_review_packet(
        local_manifest_path=first_inputs[0],
        local_source_root=first_inputs[1],
        public_lock_path=first_inputs[2],
        public_source_root=first_inputs[3],
        output_root=Path("draft"),
        allowed_output_parent=first_allowed,
    )
    second = build_engineer_review_packet(
        local_manifest_path=second_inputs[0],
        local_source_root=second_inputs[1],
        public_lock_path=second_inputs[2],
        public_source_root=second_inputs[3],
        output_root=Path("draft"),
        allowed_output_parent=second_allowed,
    )

    assert first == second
    assert first["case_count"] == 30
    assert first["takeoff_review_case_count"] == 5
    assert len(first["takeoff_review_case_ids"]) == 5
    assert all(case["case_id"] == f"sha256-{case['source_sha256']}" for case in first["cases"])
    assert all(case["production_evidence"] is False for case in first["cases"])
    assert all(case["engineer_selected"] is False for case in first["cases"])
    assert all(case["company_approved"] is False for case in first["cases"])
    assert all(case["review"]["reviewer_identity"] is None for case in first["cases"])
    local_cases = [case for case in first["cases"] if case["origin"] == "local_intake"]
    public_cases = [case for case in first["cases"] if case["origin"] == "public_fetch_lock"]
    assert len(local_cases) == 6
    assert len(public_cases) == 24
    assert all(case["source_class"] == "customer_local_unreviewed" for case in local_cases)
    assert all(case["synthetic"] is False for case in local_cases)
    assert all(case["development_fixture"] is False for case in local_cases)
    assert all(case["source_class"] == "licensed_public_development" for case in public_cases)
    assert all(case["synthetic"] is True for case in public_cases)
    assert all(case["development_fixture"] is True for case in public_cases)
    assert all(case["source_drawing"]["synthetic"] is True for case in public_cases)
    takeoffs = [case for case in first["cases"] if case["case_type"] == "takeoff"]
    assert len(takeoffs) == 5
    assert all(case["format"] == "dxf" for case in takeoffs)

    first_packet = first_allowed / "draft"
    second_packet = second_allowed / "draft"
    assert (first_packet / "engineer-review-manifest.draft.json").read_bytes() == (
        second_packet / "engineer-review-manifest.draft.json"
    ).read_bytes()
    json_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(first_packet.rglob("*.json"))
    )
    for forbidden in (
        "customer-secret-alpha",
        "confidential-part-name",
        "download-original-names",
        "original-public-name",
        "original-source-id",
        "private-intake-name",
        "qcad-flange-dxf",
        "qcad/flange.dxf",
        "raw.githubusercontent.com",
    ):
        assert forbidden not in json_text
    assert 'expected_plan":null' in json_text
    assert "engineer_human_review_pending" in json_text
    assert not any("generated-must-not-enter" in path.name for path in first_packet.rglob("*"))
    assert len(list((first_packet / "sources").iterdir())) == 30
    assert (first_packet / "production-manifest.template.json").read_bytes() == (
        second_packet / "production-manifest.template.json"
    ).read_bytes()

    for inputs in (first_inputs, second_inputs):
        for path, before_hash in inputs[4].items():
            assert _digest(path.read_bytes()) == before_hash


def test_production_template_and_claims_match_verifier_contract_without_evidence(
    tmp_path: Path,
) -> None:
    _, packet = _build(tmp_path)
    production_path = packet / "production-manifest.template.json"
    production = json.loads(production_path.read_text(encoding="utf-8"))

    assert set(production) == {
        "schema_version",
        "manifest_kind",
        "production_evidence",
        "review_status",
        "cases",
    }
    assert production["manifest_kind"] == "reviewed_production_golden_corpus"
    assert production["production_evidence"] is False
    assert production["review_status"] == "pending"
    assert len(production["cases"]) == 30
    takeoffs = [case for case in production["cases"] if case["case_type"] == "takeoff"]
    assert len(takeoffs) == 5

    expected_base_artifacts = {
        "input_spec",
        "company_profile",
        "expected_plan",
        "expected_semantic_entities",
        "expected_validation",
        "preview_reference",
    }
    expected_takeoff_artifacts = {
        "input_drawing",
        "takeoff_request",
        "expected_takeoff",
    }
    expected_claim_keys = {
        "engineer_selector": {
            "evidence_kind",
            "manifest_kind",
            "case_id",
            "source_sha256",
            "provenance_sha256",
            "source_class",
            "synthetic",
            "development_fixture",
            "source_semantic_evidence_sha256",
            "source_drawing_model_sha256",
            "artifact_sha256",
            "selected",
        },
        "golden_reviewer": {
            "evidence_kind",
            "manifest_kind",
            "case_id",
            "source_sha256",
            "provenance_sha256",
            "source_class",
            "synthetic",
            "development_fixture",
            "source_semantic_evidence_sha256",
            "source_drawing_model_sha256",
            "artifact_sha256",
            "review_evidence_sha256",
            "accepted",
        },
        "company_profile_approver": {
            "evidence_kind",
            "company_profile_ref",
            "company_profile_sha256",
            "approved",
        },
        "takeoff_calculator": {
            "evidence_kind",
            "case_id",
            "source_sha256",
            "takeoff_request_sha256",
            "expected_takeoff_sha256",
            "recomputed_takeoff_sha256",
            "calculation_evidence_sha256",
        },
        "takeoff_reviewer": {
            "evidence_kind",
            "case_id",
            "source_sha256",
            "takeoff_request_sha256",
            "expected_takeoff_sha256",
            "recomputed_takeoff_sha256",
            "calculation_evidence_sha256",
            "calculator_identity",
            "accepted",
        },
        "material_table_approver": {
            "evidence_kind",
            "case_id",
            "material_profile_ref",
            "material_table_sha256",
            "approval_evidence_sha256",
            "approved",
        },
    }
    for case in production["cases"]:
        is_takeoff = case["case_type"] == "takeoff"
        assert case["production_evidence"] is False
        assert case["review_status"] == "pending"
        assert case["engineer_selected"] is False
        assert case["company_approved"] is False
        assert case["selector_identity"] is None
        assert case["selector_attestation"] is None
        assert case["company_profile_attestation"] is None
        assert case["review"]["attestation"] is None
        assert case["source_drawing"]["provenance_ref"] is None
        assert case["source_drawing"]["provenance"] is None
        if case["format"] == "dxf":
            assert case["source_drawing"]["semantic_snapshot"] is None
            assert "drawing_model" not in case["source_drawing"]
            assert "bridge_evidence" not in case["source_drawing"]
        else:
            assert case["source_drawing"]["drawing_model"] is None
            assert case["source_drawing"]["bridge_evidence"] is None
            assert "semantic_snapshot" not in case["source_drawing"]
        assert set(case["artifacts"]) == expected_base_artifacts | (
            expected_takeoff_artifacts if is_takeoff else set()
        )
        assert all(
            value is None for name, value in case["artifacts"].items() if name != "input_drawing"
        )
        roles = {
            "engineer_selector",
            "golden_reviewer",
            "company_profile_approver",
        }
        if is_takeoff:
            roles |= {
                "takeoff_calculator",
                "takeoff_reviewer",
                "material_table_approver",
            }
            assert case["artifacts"]["input_drawing"] == {
                "artifact_ref": case["source_drawing"]["artifact_ref"],
                "sha256": case["source_drawing"]["sha256"],
            }
            assert case["takeoff"]["calculator_attestation"] is None
            assert case["takeoff"]["reviewer_attestation"] is None
            assert case["takeoff"]["material_table"]["table"] is None
            assert case["takeoff"]["material_table"]["attestation"] is None
        else:
            assert case["takeoff"] is None
        assert set(case["claim_template_refs"]) == roles
        for role, ref in case["claim_template_refs"].items():
            claims = json.loads((packet / ref).read_text(encoding="utf-8"))
            assert set(claims) == expected_claim_keys[role]
            if "selected" in claims:
                assert claims["selected"] is False
            if "accepted" in claims:
                assert claims["accepted"] is False
            if "approved" in claims:
                assert claims["approved"] is False
        workflow = json.loads(
            (packet / case["attestation_workflow_ref"]).read_text(encoding="utf-8")
        )
        assert workflow["ready_to_sign"] is False
        assert workflow["production_evidence"] is False
        assert set(workflow["roles"]) == roles
        for role, instructions in workflow["roles"].items():
            argv = instructions["issue_command_argv"]
            assert argv[0] == "scripts/issue_evidence_attestation.py"
            assert argv[argv.index("--role") + 1] == role
            assert argv[argv.index("--claims") + 1] == case["claim_template_refs"][role]
            assert argv[argv.index("--private-key-env") + 1] == (
                "<issuer-private-key-environment-variable>"
            )
            assert argv[argv.index("--expected-policy-sha256") + 1] == (
                "<pinned-canonical-policy-sha256>"
            )
    assert not list(packet.rglob("attestations/*.json"))

    policy = tmp_path / "trust-policy.json"
    policy_payload = {
        "schema_version": "2.0",
        "policy_kind": "production_evidence_trust_policy",
        "identities": [
            {
                "identity_id": "unassigned-operator",
                "allowed_roles": ["engineer_selector"],
                "public_key": base64.urlsafe_b64encode(bytes([1]) * 32)
                .rstrip(b"=")
                .decode("ascii"),
            }
        ],
    }
    _write_json(
        policy,
        policy_payload,
    )
    policy_digest = trust_policy_sha256(trust_policy_from_mapping(policy_payload))
    summary = verify_production_golden_acceptance(
        production_path,
        trust_policy_path=policy,
        trust_policy_sha256=policy_digest,
    )
    assert summary["passed"] is False
    assert {error["code"] for error in summary["errors"]} >= {
        "NOT_PRODUCTION_EVIDENCE",
        "REVIEW_STATUS_INVALID",
        "ARTIFACT_INVALID",
        "EVIDENCE_ATTESTATION_INVALID",
    }


def test_tampered_locked_source_is_rejected_without_output(tmp_path: Path) -> None:
    local_manifest, local_root, public_lock, public_root, _ = _fixture(tmp_path)
    victim = next(public_root.rglob("*.dxf"))
    victim.write_bytes(victim.read_bytes() + b"tamper")
    allowed = tmp_path / "data"
    allowed.mkdir()

    with pytest.raises(EngineerReviewPacketError, match="PUBLIC_SOURCE_LOCK_MISMATCH"):
        build_engineer_review_packet(
            local_manifest_path=local_manifest,
            local_source_root=local_root,
            public_lock_path=public_lock,
            public_source_root=public_root,
            output_root=Path("packet"),
            allowed_output_parent=allowed,
        )

    assert not (allowed / "packet").exists()


def test_local_synthetic_classification_is_rejected(tmp_path: Path) -> None:
    local_manifest, local_root, public_lock, public_root, _ = _fixture(tmp_path)
    payload = json.loads(local_manifest.read_text(encoding="utf-8"))
    payload["cases"][0]["source_kind"] = "synthetic"
    _write_json(local_manifest, payload)
    allowed = tmp_path / "data"
    allowed.mkdir()

    with pytest.raises(EngineerReviewPacketError, match="LOCAL_SOURCE_CLASSIFICATION_INVALID"):
        build_engineer_review_packet(
            local_manifest_path=local_manifest,
            local_source_root=local_root,
            public_lock_path=public_lock,
            public_source_root=public_root,
            output_root=Path("packet"),
            allowed_output_parent=allowed,
        )


def test_forged_minimal_public_lock_cannot_supply_candidates(tmp_path: Path) -> None:
    local_manifest, local_root, public_lock, public_root, _ = _fixture(tmp_path)
    public_drawing = next(public_root.rglob("*.dxf"))
    forged_source = {
        "source_id": "forged-public",
        "output": public_drawing.relative_to(public_root).as_posix(),
        "url": "https://example.invalid/forged.dxf",
        "license_id": "MIT",
    }
    _write_json(
        public_lock,
        {
            "schema_version": "1.0",
            "manifest_sha256": "a" * 64,
            "manifest": {"production_evidence": False},
            "source_count": 1,
            "sources": [
                {
                    "source": forged_source,
                    "sha256": _digest(public_drawing.read_bytes()),
                    "size_bytes": public_drawing.stat().st_size,
                }
            ],
        },
    )
    allowed = tmp_path / "data"
    allowed.mkdir()

    with pytest.raises(EngineerReviewPacketError, match="PUBLIC_LOCK_CONTRACT_MISMATCH"):
        build_engineer_review_packet(
            local_manifest_path=local_manifest,
            local_source_root=local_root,
            public_lock_path=public_lock,
            public_source_root=public_root,
            output_root=Path("packet"),
            allowed_output_parent=allowed,
        )


def test_duplicate_drawing_hash_is_rejected(tmp_path: Path) -> None:
    local_manifest, local_root, public_lock, public_root, _ = _fixture(tmp_path)
    duplicate = local_root / "same-bytes-second-name.dxf"
    duplicate.write_bytes(next(local_root.glob("confidential-*.dxf")).read_bytes())
    allowed = tmp_path / "data"
    allowed.mkdir()

    with pytest.raises(EngineerReviewPacketError, match="DUPLICATE_SOURCE_HASH"):
        build_engineer_review_packet(
            local_manifest_path=local_manifest,
            local_source_root=local_root,
            public_lock_path=public_lock,
            public_source_root=public_root,
            output_root=Path("packet"),
            allowed_output_parent=allowed,
        )


def test_reparse_drawing_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local_manifest, local_root, public_lock, public_root, _ = _fixture(tmp_path)
    allowed = tmp_path / "data"
    allowed.mkdir()
    source_inode = next(local_root.glob("confidential-*.dxf")).lstat().st_ino
    monkeypatch.setattr(
        "scripts.build_engineer_review_packet._is_reparse",
        lambda metadata: metadata.st_ino == source_inode,
    )

    with pytest.raises(EngineerReviewPacketError, match="REPARSE_POINT_NOT_ALLOWED"):
        build_engineer_review_packet(
            local_manifest_path=local_manifest,
            local_source_root=local_root,
            public_lock_path=public_lock,
            public_source_root=public_root,
            output_root=Path("packet"),
            allowed_output_parent=allowed,
        )


def test_existing_output_is_never_overwritten(tmp_path: Path) -> None:
    manifest, packet = _build(tmp_path)
    manifest_before = (packet / "engineer-review-manifest.draft.json").read_bytes()
    inputs = _fixture(tmp_path / "retry-inputs")

    with pytest.raises(EngineerReviewPacketError, match="OUTPUT_ALREADY_EXISTS"):
        build_engineer_review_packet(
            local_manifest_path=inputs[0],
            local_source_root=inputs[1],
            public_lock_path=inputs[2],
            public_source_root=inputs[3],
            output_root=packet,
            allowed_output_parent=packet.parent,
        )

    assert manifest["case_count"] == 30
    assert (packet / "engineer-review-manifest.draft.json").read_bytes() == manifest_before


def test_fewer_than_thirty_unique_drawings_is_rejected(tmp_path: Path) -> None:
    local_manifest, local_root, public_lock, public_root, _ = _fixture(
        tmp_path, local_count=5, add_excluded=False
    )
    allowed = tmp_path / "data"
    allowed.mkdir()

    with pytest.raises(EngineerReviewPacketError, match="CASE_COUNT_OUT_OF_RANGE"):
        build_engineer_review_packet(
            local_manifest_path=local_manifest,
            local_source_root=local_root,
            public_lock_path=public_lock,
            public_source_root=public_root,
            output_root=Path("packet"),
            allowed_output_parent=allowed,
        )


def test_output_escape_is_rejected(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    allowed = tmp_path / "data"
    allowed.mkdir()

    with pytest.raises(EngineerReviewPacketError, match="OUTPUT_PATH_NOT_ALLOWED"):
        build_engineer_review_packet(
            local_manifest_path=inputs[0],
            local_source_root=inputs[1],
            public_lock_path=inputs[2],
            public_source_root=inputs[3],
            output_root=Path("..") / "outside",
            allowed_output_parent=allowed,
        )
