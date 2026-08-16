# Changelog

All notable user-visible changes will be documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases will follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) once the first public version is
tagged.

## Unreleased

## 0.3.0 - 2026-08-16

### Added

- Bounded `reference_circle` geometry and one-call `cad_change_prepare` planning.
- Planning-only MCP permission mode and adapter-specific live setup evidence.
- Safe adoption and Alembic migration for trusted legacy v0.2.2 SQLite databases.
- Apache-2.0 open-source licensing and contributor governance.
- Public contribution, security, support, community, issue and pull-request policies.

### Changed

- Public contract schema advanced to 1.13; old plans require recompilation and approval.
- COM planning now asks only for the applicable company-standards confirmation.
- Plan, validation, profile, revision and post-readback bindings fail closed before writes.
- README onboarding now states safety boundaries, current evidence and known limitations.

### Fixed

- Identical deterministic plan hashes can be persisted for separate jobs.
- Explicit approved entity layers remain auditable after commit.
- Circle preview/readback accepts the canonical diameter representation consistently.
