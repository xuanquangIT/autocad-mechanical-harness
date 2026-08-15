"""Pinned public-corpus acquisition is bounded, lawful, and reproducible."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Self
from urllib.request import Request

import pytest
import yaml
from scripts.fetch_development_corpus import (
    CONFIG_PATH,
    LOCK_FILENAME,
    CorpusFetchError,
    check_development_corpus,
    fetch_development_corpus,
    load_manifest,
)

DXF = b"  0\nSECTION\n  2\nHEADER\n  0\nENDSEC\n  0\nEOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture"
PDF = b"%PDF-1.7\nfixture\n%%EOF\n"


class FakeResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        final_url: str,
        content_length: str | None = None,
    ) -> None:
        self._payload = payload
        self._offset = 0
        self._final_url = final_url
        self.headers = {} if content_length is None else {"Content-Length": content_length}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def geturl(self) -> str:
        return self._final_url

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self._payload) - self._offset
        chunk = self._payload[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk


class FakeOpener:
    def __init__(self, payloads: dict[str, bytes], *, final_url: str | None = None) -> None:
        self.payloads = payloads
        self.final_url = final_url
        self.calls: list[tuple[str, float]] = []

    def __call__(self, request: Request, *, timeout: float) -> FakeResponse:
        self.calls.append((request.full_url, timeout))
        payload = self.payloads[request.full_url]
        return FakeResponse(
            payload,
            final_url=self.final_url or request.full_url,
            content_length=str(len(payload)),
        )


def _source(
    *,
    source_id: str = "qcad-fixture-dxf",
    url: str = "https://raw.githubusercontent.com/qcad/qcad/master/examples/flange.dxf",
    output: str = "qcad/flange.dxf",
    max_bytes: int = 1024,
) -> dict[str, object]:
    return {
        "source_id": source_id,
        "url": url,
        "source_index_url": "https://github.com/qcad/qcad/tree/master/examples",
        "license_id": "GPL-3.0-only",
        "license_url": "https://github.com/qcad/qcad/blob/master/LICENSE.txt",
        "license_notice": "Retain the repository license terms.",
        "attribution": "QCAD contributors",
        "output": output,
        "max_bytes": max_bytes,
        "intended_use": "Offline test fixture.",
        "expected_sha256": hashlib.sha256(DXF).hexdigest(),
    }


def _write_manifest(path: Path, sources: list[dict[str, object]]) -> Path:
    payload = {
        "schema_version": "1.0",
        "corpus_id": "test-development-corpus",
        "purpose": "Unit-test-only public corpus.",
        "customer_inputs_allowed": False,
        "production_evidence": False,
        "sources": sources,
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_repository_manifest_pins_expected_lawful_sources() -> None:
    manifest = load_manifest(CONFIG_PATH)
    urls = {source.url for source in manifest.sources}

    assert len(manifest.sources) == 26
    assert sum(source.output.endswith(".dxf") for source in manifest.sources) == 24
    assert all(source.expected_sha256 is not None for source in manifest.sources)
    assert urls == {
        "https://raw.githubusercontent.com/qcad/qcad/3e49b22539a35af9b461a2438c080219e8f0dbd3/examples/flange.dxf",
        "https://raw.githubusercontent.com/qcad/qcad/3e49b22539a35af9b461a2438c080219e8f0dbd3/examples/projection.dxf",
        "https://raw.githubusercontent.com/qcad/qcad/3e49b22539a35af9b461a2438c080219e8f0dbd3/examples/entities.dxf",
        "https://raw.githubusercontent.com/qcad/qcad/3e49b22539a35af9b461a2438c080219e8f0dbd3/examples/calibration.dxf",
        "https://raw.githubusercontent.com/qcad/qcad/3e49b22539a35af9b461a2438c080219e8f0dbd3/examples/example00.dxf",
        "https://raw.githubusercontent.com/qcad/qcad/3e49b22539a35af9b461a2438c080219e8f0dbd3/examples/example01.dxf",
        "https://raw.githubusercontent.com/qcad/qcad/3e49b22539a35af9b461a2438c080219e8f0dbd3/examples/isometric_grid.dxf",
        "https://raw.githubusercontent.com/qcad/qcad/3e49b22539a35af9b461a2438c080219e8f0dbd3/examples/linetypes.dxf",
        "https://raw.githubusercontent.com/qcad/qcad/3e49b22539a35af9b461a2438c080219e8f0dbd3/examples/lineweights.dxf",
        "https://raw.githubusercontent.com/mozman/ezdxf/b3eb37b942acb4c7e2d2487706e614aa29b7f9b2/examples_dxf/dimension_in_block.dxf",
        "https://raw.githubusercontent.com/mozman/ezdxf/b3eb37b942acb4c7e2d2487706e614aa29b7f9b2/examples_dxf/dimension_in_nested_blocks.dxf",
        "https://raw.githubusercontent.com/mozman/ezdxf/b3eb37b942acb4c7e2d2487706e614aa29b7f9b2/examples_dxf/multi_insert_with_attribs.dxf",
        "https://raw.githubusercontent.com/mozman/ezdxf/b3eb37b942acb4c7e2d2487706e614aa29b7f9b2/examples_dxf/hatches_1.dxf",
        "https://raw.githubusercontent.com/mozman/ezdxf/b3eb37b942acb4c7e2d2487706e614aa29b7f9b2/examples_dxf/text_alignments.dxf",
        "https://raw.githubusercontent.com/mozman/ezdxf/b3eb37b942acb4c7e2d2487706e614aa29b7f9b2/examples_dxf/uncommon.dxf",
        "https://raw.githubusercontent.com/mozman/ezdxf/b3eb37b942acb4c7e2d2487706e614aa29b7f9b2/examples_dxf/3dface.dxf",
        "https://raw.githubusercontent.com/mozman/ezdxf/b3eb37b942acb4c7e2d2487706e614aa29b7f9b2/examples_dxf/acad_table_simple.dxf",
        "https://raw.githubusercontent.com/mozman/ezdxf/b3eb37b942acb4c7e2d2487706e614aa29b7f9b2/examples_dxf/columns_R2018.dxf",
        "https://raw.githubusercontent.com/mozman/ezdxf/b3eb37b942acb4c7e2d2487706e614aa29b7f9b2/examples_dxf/hatches_2.dxf",
        "https://raw.githubusercontent.com/mozman/ezdxf/b3eb37b942acb4c7e2d2487706e614aa29b7f9b2/examples_dxf/insert_bricscad_level_1.dxf",
        "https://raw.githubusercontent.com/mozman/ezdxf/b3eb37b942acb4c7e2d2487706e614aa29b7f9b2/examples_dxf/text.dxf",
        "https://raw.githubusercontent.com/mozman/ezdxf/b3eb37b942acb4c7e2d2487706e614aa29b7f9b2/examples_dxf/visibility.dxf",
        "https://raw.githubusercontent.com/mozman/ezdxf/b3eb37b942acb4c7e2d2487706e614aa29b7f9b2/examples_dxf/wipeout_door.dxf",
        "https://raw.githubusercontent.com/mozman/ezdxf/b3eb37b942acb4c7e2d2487706e614aa29b7f9b2/examples_dxf/xclip.dxf",
        "https://raw.githubusercontent.com/qcad/qcad/3e49b22539a35af9b461a2438c080219e8f0dbd3/examples/flange.png",
        "https://archive.org/download/practicalproblem00stur/practicalproblem00stur.pdf",
    }
    for source in manifest.sources:
        assert source.metadata["source_index_url"].startswith("https://")
        assert source.metadata["license_id"]
        assert source.metadata["license_url"].startswith("https://")
        assert source.metadata["license_notice"]
        assert source.metadata["attribution"]
    public_domain = next(source for source in manifest.sources if source.output.endswith(".pdf"))
    assert public_domain.metadata["license_id"] == "Public-Domain-US"
    assert public_domain.metadata["rights_url"] == "https://www.loc.gov/item/22002448/"
    assert public_domain.metadata["selected_page_raster_derivation_only"] is True


def test_first_fetch_writes_atomic_hash_lock_and_check_is_network_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_manifest(tmp_path / "manifest.yaml", [_source()])
    output = tmp_path / "corpus"
    url = "https://raw.githubusercontent.com/qcad/qcad/master/examples/flange.dxf"
    opener = FakeOpener({url: DXF})

    lock = fetch_development_corpus(
        output,
        config_path=config,
        opener=opener,
        allowed_output_parent=tmp_path,
    )

    assert (output / "qcad" / "flange.dxf").read_bytes() == DXF
    assert lock["sources"][0]["sha256"] == hashlib.sha256(DXF).hexdigest()
    assert lock["sources"][0]["source"]["license_notice"]
    assert json.loads((output / LOCK_FILENAME).read_text(encoding="utf-8")) == lock
    assert len(opener.calls) == 1

    def network_must_not_be_constructed(*args: object, **kwargs: object) -> None:
        raise AssertionError("--check attempted network access")

    monkeypatch.setattr("urllib.request.build_opener", network_must_not_be_constructed)
    checked = check_development_corpus(
        output,
        config_path=config,
        allowed_output_parent=tmp_path,
    )

    assert checked == lock
    assert len(opener.calls) == 1


def test_redirect_to_non_allowlisted_host_is_rejected_without_artifact(tmp_path: Path) -> None:
    config = _write_manifest(tmp_path / "manifest.yaml", [_source()])
    output = tmp_path / "corpus"
    url = "https://raw.githubusercontent.com/qcad/qcad/master/examples/flange.dxf"
    opener = FakeOpener({url: DXF}, final_url="https://example.com/customer.dxf")

    with pytest.raises(CorpusFetchError, match="allowlisted"):
        fetch_development_corpus(
            output,
            config_path=config,
            opener=opener,
            allowed_output_parent=tmp_path,
        )

    assert not (output / "qcad" / "flange.dxf").exists()
    assert not (output / LOCK_FILENAME).exists()


def test_archive_org_download_node_redirect_is_allowed(tmp_path: Path) -> None:
    source = _source(
        source_id="loc-public-domain-pdf",
        url="https://archive.org/download/book/book.pdf",
        output="public-domain/book.pdf",
    )
    source["license_id"] = "Public-Domain-US"
    source["expected_sha256"] = hashlib.sha256(PDF).hexdigest()
    config = _write_manifest(tmp_path / "manifest.yaml", [source])
    output = tmp_path / "corpus"
    opener = FakeOpener(
        {str(source["url"]): PDF},
        final_url="https://dn721700.ca.archive.org/0/items/book/book.pdf",
    )

    fetch_development_corpus(
        output,
        config_path=config,
        opener=opener,
        allowed_output_parent=tmp_path,
    )

    assert (output / "public-domain" / "book.pdf").read_bytes() == PDF


def test_size_and_magic_are_checked_before_atomic_replace(tmp_path: Path) -> None:
    config = _write_manifest(tmp_path / "manifest.yaml", [_source(max_bytes=8)])
    output = tmp_path / "corpus"
    url = "https://raw.githubusercontent.com/qcad/qcad/master/examples/flange.dxf"

    with pytest.raises(CorpusFetchError, match="max_bytes"):
        fetch_development_corpus(
            output,
            config_path=config,
            opener=FakeOpener({url: DXF}),
            allowed_output_parent=tmp_path,
        )
    assert list(output.rglob("*.tmp")) == []
    assert not (output / "qcad" / "flange.dxf").exists()

    config = _write_manifest(tmp_path / "manifest.yaml", [_source(max_bytes=1024)])
    with pytest.raises(CorpusFetchError, match="magic"):
        fetch_development_corpus(
            output,
            config_path=config,
            opener=FakeOpener({url: PNG}),
            allowed_output_parent=tmp_path,
        )
    assert list(output.rglob("*.tmp")) == []
    assert not (output / "qcad" / "flange.dxf").exists()


def test_existing_lock_rejects_upstream_drift_without_replacing_artifact(tmp_path: Path) -> None:
    config = _write_manifest(tmp_path / "manifest.yaml", [_source()])
    output = tmp_path / "corpus"
    url = "https://raw.githubusercontent.com/qcad/qcad/master/examples/flange.dxf"
    fetch_development_corpus(
        output,
        config_path=config,
        opener=FakeOpener({url: DXF}),
        allowed_output_parent=tmp_path,
    )
    changed = DXF.replace(b"HEADER", b"TABLES")

    with pytest.raises(CorpusFetchError, match=r"expected_sha256|existing lock"):
        fetch_development_corpus(
            output,
            config_path=config,
            opener=FakeOpener({url: changed}),
            allowed_output_parent=tmp_path,
        )

    assert (output / "qcad" / "flange.dxf").read_bytes() == DXF


@pytest.mark.parametrize(
    ("url", "output"),
    [
        (
            "http://raw.githubusercontent.com/qcad/qcad/master/examples/flange.dxf",
            "qcad/flange.dxf",
        ),
        ("https://example.com/flange.dxf", "qcad/flange.dxf"),
        (
            "https://raw.githubusercontent.com/qcad/qcad/master/examples/flange.dxf",
            "../customer.dxf",
        ),
        (
            "https://raw.githubusercontent.com/qcad/qcad/master/examples/flange.dxf",
            "qcad/flange.png",
        ),
    ],
)
def test_manifest_rejects_arbitrary_urls_and_paths(tmp_path: Path, url: str, output: str) -> None:
    config = _write_manifest(tmp_path / "manifest.yaml", [_source(url=url, output=output)])

    with pytest.raises(CorpusFetchError):
        load_manifest(config)


def test_output_root_must_remain_under_explicit_ignored_parent(tmp_path: Path) -> None:
    config = _write_manifest(tmp_path / "manifest.yaml", [_source()])
    url = "https://raw.githubusercontent.com/qcad/qcad/master/examples/flange.dxf"

    with pytest.raises(CorpusFetchError, match="ignored data tree"):
        fetch_development_corpus(
            tmp_path.parent / "outside-corpus",
            config_path=config,
            opener=FakeOpener({url: DXF}),
            allowed_output_parent=tmp_path,
        )


def test_check_fails_when_manifest_or_local_artifact_diverges_from_lock(tmp_path: Path) -> None:
    config = _write_manifest(tmp_path / "manifest.yaml", [_source()])
    output = tmp_path / "corpus"
    url = "https://raw.githubusercontent.com/qcad/qcad/master/examples/flange.dxf"
    fetch_development_corpus(
        output,
        config_path=config,
        opener=FakeOpener({url: DXF}),
        allowed_output_parent=tmp_path,
    )
    (output / "qcad" / "flange.dxf").write_bytes(DXF + b"0\n")

    with pytest.raises(CorpusFetchError, match="integrity"):
        check_development_corpus(
            output,
            config_path=config,
            allowed_output_parent=tmp_path,
        )

    (output / "qcad" / "flange.dxf").write_bytes(DXF)
    payload = yaml.safe_load(config.read_text(encoding="utf-8"))
    payload["purpose"] = "Changed after locking."
    config.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    with pytest.raises(CorpusFetchError, match="exactly match"):
        check_development_corpus(
            output,
            config_path=config,
            allowed_output_parent=tmp_path,
        )
