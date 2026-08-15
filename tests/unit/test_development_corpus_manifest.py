"""Privacy and determinism gates for development-corpus intake."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import ezdxf
import pytest
from scripts import build_development_corpus_manifest as corpus_intake
from scripts.build_development_corpus_manifest import (
    CorpusIntakeError,
    build_development_corpus_manifest,
    main,
    render_manifest,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_dxf(path: Path) -> None:
    document = ezdxf.new("R2018")
    document.header["$INSUNITS"] = 4
    document.modelspace().add_line((0, 0), (10, 0))
    document.saveas(path)


def _write_public_provenance(path: Path, relative: str, expected_hash: str) -> None:
    path.write_text(
        "\n".join(
            [
                'schema_version: "1.0"',
                "files:",
                f'  "{relative}":',
                "    source_url: https://example.org/cad/fixture.dxf",
                "    license_id: CC-BY-4.0",
                "    license_url: https://creativecommons.org/licenses/by/4.0/",
                '    retrieved_at: "2026-08-15T00:00:00Z"',
                f'    expected_sha256: "{expected_hash}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_manifest_is_deterministic_redacted_and_source_is_unchanged(tmp_path: Path) -> None:
    corpus = tmp_path / "customer-secret-root"
    nested = corpus / "Client Alpha Project"
    nested.mkdir(parents=True)
    dxf_path = nested / "confidential bracket.dxf"
    dwg_path = corpus / "private assembly.dwg"
    _write_dxf(dxf_path)
    dwg_path.write_bytes(b"AC1032" + b"\x00" * 64)
    ignored = corpus / "notes.txt"
    ignored.write_text("not drawing intake", encoding="utf-8")
    before = {
        path: (path.stat().st_mtime_ns, _sha256(path)) for path in (dxf_path, dwg_path, ignored)
    }

    first = render_manifest(build_development_corpus_manifest(corpus))
    second = render_manifest(build_development_corpus_manifest(corpus))

    assert first == second
    assert str(corpus) not in first
    assert "customer-secret-root" not in first
    assert "Client Alpha Project" not in first
    assert "confidential bracket.dxf" not in first
    assert "private assembly.dwg" not in first
    payload = json.loads(first)
    assert [case["case_id"] for case in payload["cases"]] == ["case-0001", "case-0002"]
    assert {case["source_kind"] for case in payload["cases"]} == {"customer_local"}
    assert payload["privacy"] == {
        "absolute_paths_omitted": True,
        "local_labels_included": False,
        "source_filenames_omitted": True,
    }
    assert {
        path: (path.stat().st_mtime_ns, _sha256(path)) for path in (dxf_path, dwg_path, ignored)
    } == before


def test_explicit_metadata_classifies_public_generated_and_customer_sources(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    public_path = corpus / "public.dxf"
    generated_path = corpus / "generated.png"
    customer_path = corpus / "customer.jpg"
    _write_dxf(public_path)
    generated_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"synthetic")
    customer_path.write_bytes(b"\xff\xd8\xff" + b"local")
    provenance = tmp_path / "provenance.yaml"
    _write_public_provenance(provenance, "public.dxf", _sha256(public_path))
    labels = tmp_path / "labels.yaml"
    labels.write_text(
        """schema_version: "1.0"
files:
  generated.png:
    label: Synthetic scan 001
    source_kind: generated
""",
        encoding="utf-8",
    )

    payload = build_development_corpus_manifest(
        corpus,
        public_provenance_path=provenance,
        local_label_map_path=labels,
    )

    by_kind = {case["source_kind"]: case for case in payload["cases"]}
    assert set(by_kind) == {"public_licensed", "generated", "customer_local"}
    assert by_kind["generated"]["local_label"] == "Synthetic scan 001"
    assert by_kind["public_licensed"]["public_provenance"]["license_id"] == "CC-BY-4.0"
    rendered = render_manifest(payload)
    for forbidden_claim in ("engineer_selected", "company_approved", "reviewer_identity"):
        assert forbidden_claim not in rendered
    assert payload["production_claim_eligible"] is False


def test_symlink_or_reparse_entry_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = tmp_path / "outside.dxf"
    source.write_bytes(b"0\nSECTION\n")
    link = corpus / "escape.dxf"
    try:
        link.symlink_to(source)
    except OSError:
        safe = corpus / "regular.dxf"
        safe.write_bytes(b"0\nSECTION\n")
        original = corpus_intake._is_reparse
        calls = 0

        def is_reparse_after_root(metadata: os.stat_result) -> bool:
            nonlocal calls
            calls += 1
            return calls > 1 or original(metadata)

        monkeypatch.setattr(corpus_intake, "_is_reparse", is_reparse_after_root)

    with pytest.raises(CorpusIntakeError, match="REPARSE_POINT_NOT_ALLOWED"):
        build_development_corpus_manifest(corpus)


def test_output_path_escape_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = tmp_path / "corpus"
    output_root = tmp_path / "allowed"
    corpus.mkdir()
    output_root.mkdir()
    (corpus / "part.dwg").write_bytes(b"AC1032")
    escaped = tmp_path / "outside.json"

    result = main(
        [
            str(corpus),
            "--output",
            str(escaped),
            "--output-root",
            str(output_root),
        ]
    )

    assert result == 2
    assert not escaped.exists()
    assert json.loads(capsys.readouterr().err) == {"error": {"code": "OUTPUT_PATH_NOT_ALLOWED"}}


def test_public_provenance_wrong_hash_fails_closed(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "part.dxf"
    _write_dxf(source)
    provenance = tmp_path / "provenance.yaml"
    _write_public_provenance(provenance, "part.dxf", "0" * 64)

    with pytest.raises(CorpusIntakeError, match="PUBLIC_HASH_MISMATCH"):
        build_development_corpus_manifest(corpus, public_provenance_path=provenance)


def test_public_provenance_missing_license_fails_closed(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "part.dxf"
    _write_dxf(source)
    provenance = tmp_path / "provenance.yaml"
    provenance.write_text(
        "\n".join(
            [
                "files:",
                "  part.dxf:",
                "    source_url: https://example.org/cad/part.dxf",
                "    license_url: https://creativecommons.org/licenses/by/4.0/",
                '    retrieved_at: "2026-08-15T00:00:00Z"',
                f"    expected_sha256: {_sha256(source)}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(CorpusIntakeError, match="PUBLIC_PROVENANCE_INVALID"):
        build_development_corpus_manifest(corpus, public_provenance_path=provenance)


@pytest.mark.parametrize(
    ("filename", "content", "declared", "detected", "status", "signature"),
    [
        ("a.dwg", b"AC1032payload", "dwg", "dwg", "recognized", "AC1032"),
        (
            "b.dxf",
            b"AutoCAD Binary DXF\r\n\x1a\x00payload",
            "dxf",
            "dxf",
            "recognized",
            "BINARY_DXF",
        ),
        ("c.png", b"\x89PNG\r\n\x1a\npayload", "png", "png", "recognized", "PNG"),
        ("d.jpg", b"\xff\xd8\xffpayload", "jpeg", "jpeg", "recognized", "JPEG"),
        ("e.tif", b"II*\x00payload", "tiff", "tiff", "recognized", "TIFF_LE"),
        (
            "mismatch.dxf",
            b"\x89PNG\r\n\x1a\npayload",
            "dxf",
            "png",
            "extension_mismatch",
            "PNG",
        ),
    ],
)
def test_header_classification(
    tmp_path: Path,
    filename: str,
    content: bytes,
    declared: str,
    detected: str,
    status: str,
    signature: str,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / filename).write_bytes(content)

    manifest = build_development_corpus_manifest(corpus)
    header = manifest["cases"][0]["format"]

    assert header["declared_format"] == declared
    assert header["detected_format"] == detected
    assert header["header_status"] == status
    assert header["signature"] == signature


def test_allowed_output_is_canonical_and_never_overwritten(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    output_root = tmp_path / "output"
    corpus.mkdir()
    output_root.mkdir()
    (corpus / "part.dwg").write_bytes(b"AC1032")

    assert main([str(corpus), "--output", "manifest.json", "--output-root", str(output_root)]) == 0
    output = output_root / "manifest.json"
    first = output.read_bytes()
    assert first.endswith(b"\n")
    assert main([str(corpus), "--output", "manifest.json", "--output-root", str(output_root)]) == 2
    assert output.read_bytes() == first
