# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Seven read-only commands, all served by the headless AutoCAD core engine
  (`accoreconsole`) with `/readonly`, so none can disturb a drawing open in the
  GUI: `drawing info`, `layer list`, `entity summary`, `block list`,
  `text extract`, `layout list` and `xref list`. Exercised across all 96
  drawings shipped with AutoCAD 2026 with no engine failure and no run over
  eight seconds.
- `entity summary` aggregates counts inside AutoLISP rather than emitting one
  record per object, so a 2400-object drawing returns a histogram, not a
  transfer. It states its own scope: model and paper space, with block
  definition contents not expanded.
- `block list` reports how many times each definition is actually placed, plus
  attribute and xref state. Zero insertions means an unused block, which is what
  an audit is looking for.
- `text extract` returns TEXT/MTEXT/ATTDEF strings with layer and insertion
  point, keeping MTEXT inline formatting codes rather than guessing at them.
- `layout list` reports each sheet's paper size, plot device and plot scale.
  Model is included and flagged rather than filtered out, so a sheet count is
  never silently off by one.
- `xref list` reports external references, whether AutoCAD resolved each one,
  and whether the referenced file is actually on disk. Relative paths resolve
  against the host drawing's directory.
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
