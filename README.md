# Lanser-CLI

### Language Server CLI Empowers Language Agents with Process Rewards

`Lanser-CLI` is a CLI-first orchestration layer that lets coding agents and developers access
language server capabilities with deterministic, replayable workflows. 

## CLI Overview

- `lanser def`, `lanser references`, `lanser hover`, `lanser symbols`, and `lanser diagnostics`
  execute against Pyright to emit deterministic analysis bundles with caching
  metadata and selector resolution details.
- `lanser rename` previews rename results with guardrails for dirty worktrees and
  workspace jail enforcement.
- `lanser batch` executes multiple orchestrator commands from a JSONL description,
  writing JSONL responses so agent planners can pipeline definition, reference,
  diagnostics, or rename stubs.
- `lanser schema validate` validates JSON payloads against published schema
  definitions so agents can verify bundles and environment metadata.
- `lanser schema validate-batch` validates directories of JSON payloads so
  golden fixtures remain aligned with published schemas during automation.
- `lanser schema export` writes JSON schema definitions to disk so CI pipelines
  can vendor deterministic contracts alongside bundle fixtures.
- Pass `--trace-file <path>` to any command to capture orchestrator metadata and
  JSON-RPC traffic as JSONL events for replay and auditing.
- `lanser trace replay` reconstructs recorded operation outputs from a trace log,
  regenerating the exact JSON envelope produced during the original run.
- `lanser trace list` enumerates recorded operations with filtering (operation,
  selector text, exit code, or status) and JSON output so agents can discover replay indexes
  before reproducing results.
- `lanser trace show` displays recorded operation metadata so agents can audit
  payloads before replaying or exporting results, with the same selector and
  status filtering available for precise targeting.

## Tooling

- Pyright `1.1.406` is the pinned language server version for development and
  CI. The version is centralised in `lanser.pyright_version` and surfaced via the
  `lanser doctor` command.
