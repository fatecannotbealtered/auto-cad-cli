# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Repository seeded from `ai-native-cli-spec` at the pinned tag `v1.6.2`: the
  eight `.agent/` behavioural specs, `contract/contract.json` and the
  sync/check/codegen tooling are vendored byte-identical to that tag.
- Machine contract core: the success/failure envelope, the canonical
  `E_* -> exit code -> retryable` table generated into `auto_cad_cli/contract_gen.py`,
  and the stdout/stderr split.
- Self-describing commands `reference`, `context`, `doctor` and `changelog`,
  plus the global flags `--format`, `--json`, `--compact`, `--fields` and `--quiet`.

### Changed

### Fixed

### Deprecated

### Removed

### Security

- Declared risk tier **T1**: the tool will write drawing files and drive a
  licensed AutoCAD session on the operator's machine. Writes default to
  read-only until a human grants permission.

<!--
Copy the block below for each release. Newest version first.
Keep the link references at the bottom of the file in sync.

## [0.1.0] - YYYY-MM-DD

### Added

- First public release.

### Changed

### Fixed

### Deprecated

### Removed

### Security

[Unreleased]: https://github.com/fatecannotbealtered/auto-cad-cli/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/fatecannotbealtered/auto-cad-cli/releases/tag/v0.1.0
-->
