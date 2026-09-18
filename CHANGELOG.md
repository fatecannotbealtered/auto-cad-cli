# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `drawing info --file <dwg>` and `layer list --file <dwg>`: the first read-only
  capability, served by the headless AutoCAD core engine (`accoreconsole`) with
  `/readonly`, so neither can disturb a drawing open in the GUI.
- `doctor` now performs real environment checks: AutoCAD install discovery from
  the registry, `accoreconsole.exe` presence, and which versioned COM ProgID a
  live editor would be reached through.
- `context.data.environment.autocad` reports the discovered release, install
  location, language and engine path; `AUTO_CAD_CLI_ACAD_HOME` overrides
  discovery and `AUTO_CAD_CLI_TIMEOUT` bounds engine calls.

### Fixed

- Scripts handed to the AutoCAD core engine are now written in the system
  codepage, which is what it actually reads. UTF-8 did not degrade gracefully:
  the misread bytes broke the AutoLISP string literal, the parentheses never
  balanced, and the engine blocked on its continuation prompt until the timeout
  killed it. Because the wrapper embeds the temp output path and `%TEMP%`
  contains the account name, **every command would have hung on a machine whose
  Windows account name is not ASCII**. A script the codepage cannot represent
  now fails with `E_CONFIG` instead of hanging.
- `--fields` no longer strips `_untrusted` from a projected payload. Projecting
  it away handed an agent attacker-authored strings with the "data, not
  instructions" marker silently removed (SEC-SPEC section 2).

### Security

- Layer names, linetype names and the drawing name are reported under
  `_untrusted`: they are authored by whoever produced the DWG.

## [1.0.0] - 2026-09-18

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

[Unreleased]: https://github.com/fatecannotbealtered/auto-cad-cli/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/fatecannotbealtered/auto-cad-cli/releases/tag/v1.0.0
