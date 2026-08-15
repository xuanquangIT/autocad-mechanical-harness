"""Issue one pinned-policy Ed25519 production evidence attestation.

The private key is read only from an issuer-side environment-variable mapping;
the public trust policy contains no signing material or private-key locator.
Neither the key, identity, paths nor claims are printed to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cad_harness.security.evidence_attestation import (
    EvidenceAttestationError,
    EvidenceRole,
    issue_attestation,
    trust_policy_from_mapping,
    trust_policy_sha256,
    verify_trust_policy_digest,
)

_MAX_POLICY_BYTES = 1024 * 1024
_MAX_CLAIMS_BYTES = 262_144
TRUST_POLICY_SHA256_ENV = "CAD_HARNESS_EVIDENCE_TRUST_POLICY_SHA256"
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z", re.ASCII)


def _expiry_hours(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 720") from None
    if not 1 <= parsed <= 720:
        raise argparse.ArgumentTypeError("must be from 1 to 720")
    return parsed


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _is_reparse(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _file_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_mode),
    )


def _read_json(path: Path, *, maximum_bytes: int) -> object:
    """Read one stable regular-file snapshot through a single descriptor."""
    try:
        path_before = os.lstat(path)
        if (
            not stat.S_ISREG(path_before.st_mode)
            or _is_reparse(path_before)
            or not 0 < path_before.st_size <= maximum_bytes
        ):
            raise ValueError("unsafe evidence input")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise ValueError("unsafe evidence input") from exc
    try:
        handle_before = os.fstat(descriptor)
        if _file_signature(handle_before) != _file_signature(path_before):
            raise ValueError("evidence input changed")
        remaining = int(handle_before.st_size)
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("evidence input changed")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("evidence input changed")
        handle_after = os.fstat(descriptor)
        path_after = os.lstat(path)
        if (
            _file_signature(handle_before) != _file_signature(handle_after)
            or _file_signature(handle_after) != _file_signature(path_after)
            or _is_reparse(path_after)
        ):
            raise ValueError("evidence input changed")
        raw = b"".join(chunks)
        if len(raw) != handle_after.st_size:
            raise ValueError("evidence input changed")
    except OSError as exc:
        raise ValueError("evidence input changed") from exc
    finally:
        os.close(descriptor)
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)


def issue_from_files(
    *,
    policy_path: Path,
    claims_path: Path,
    output_path: Path,
    identity_id: str,
    role: EvidenceRole,
    private_key_env_var: str,
    expected_policy_sha256: str,
    expires_in_hours: int | None,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Issue one attestation without exposing secret or claim material."""
    policy = trust_policy_from_mapping(_read_json(policy_path, maximum_bytes=_MAX_POLICY_BYTES))
    verify_trust_policy_digest(policy, expected_policy_sha256)
    claims = _read_json(claims_path, maximum_bytes=_MAX_CLAIMS_BYTES)
    identity = next(
        (candidate for candidate in policy.identities if candidate.identity_id == identity_id),
        None,
    )
    if identity is None:
        raise ValueError("identity is not trusted")
    if _ENV_NAME.fullmatch(private_key_env_var) is None:
        raise ValueError("private key environment mapping is invalid")
    private_key = (env if env is not None else os.environ).get(private_key_env_var)
    issued_at = (now or datetime.now(UTC)).astimezone(UTC)
    expires_at = None if expires_in_hours is None else issued_at + timedelta(hours=expires_in_hours)
    attestation = issue_attestation(
        claims,  # type: ignore[arg-type]
        identity,
        role,
        private_key or "",
        issued_at=issued_at,
        expires_at=expires_at,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(attestation.to_external_dict(), stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise ValueError("attestation output already exists") from None
    return {
        "ok": True,
        "role": role.value,
        "claims_sha256": attestation.claims_sha256,
        "policy_sha256": trust_policy_sha256(policy),
        "output_written": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trust-policy", type=Path, required=True)
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--identity-id", required=True)
    parser.add_argument("--role", choices=[role.value for role in EvidenceRole], required=True)
    parser.add_argument(
        "--private-key-env",
        required=True,
        help="issuer-only environment variable containing the raw Ed25519 private key",
    )
    parser.add_argument(
        "--expected-policy-sha256",
        help=f"pinned canonical policy digest; defaults to {TRUST_POLICY_SHA256_ENV}",
    )
    parser.add_argument(
        "--expires-hours",
        type=_expiry_hours,
        help="optional validity window; omit for durable archival evidence",
    )
    args = parser.parse_args(argv)
    try:
        result = issue_from_files(
            policy_path=args.trust_policy,
            claims_path=args.claims,
            output_path=args.output,
            identity_id=args.identity_id,
            role=EvidenceRole(args.role),
            private_key_env_var=args.private_key_env,
            expected_policy_sha256=(
                args.expected_policy_sha256 or os.environ.get(TRUST_POLICY_SHA256_ENV, "")
            ),
            expires_in_hours=args.expires_hours,
        )
    except (EvidenceAttestationError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
        result = {"ok": False, "code": "EVIDENCE_ATTESTATION_ISSUE_FAILED"}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
