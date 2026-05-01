# Lanser-CLI 

[![arXiv](https://img.shields.io/badge/arXiv-2510.22907-b31b1b.svg)](https://arxiv.org/abs/2510.22907)
[![Website](https://img.shields.io/badge/Project-Website-blue)](https://yifanzhang-pro.github.io/lanser-cli)
![Python 3.12+](https://img.shields.io/badge/python-3.12-green.svg)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-yellow.svg)](https://opensource.org/license/apache-2-0)

## Reinforcement Learning from Compiler and Language Server Feedback

`Lanser-CLI` is a CLI-first orchestration layer for grounding coding agents in
compiler and language server feedback. It turns diagnostics, symbol resolution,
types, references, refactoring preconditions, and edit outcomes into
deterministic, replayable artifacts for agents and CI.

Lanser-CLI is the reference implementation for **Reinforcement Learning from
Compiler and Language Server Feedback** (**RLCSF**): a process-supervision
framework that scores intermediate tool interactions rather than waiting only
for terminal pass/fail signals. Each tool request produces an analysis bundle;
adjacent bundles can be compared to compute a shaped reward from deterministic
changes in diagnostics, selector confidence, safety readiness, and structured
tool errors.

## Why Lanser-CLI

Coding agents fail when text-level guesses outrun program facts: they
hallucinate APIs, drift to the wrong symbol, and apply edits without evidence
that the workspace remains valid. Lanser-CLI gives agents protocol grounding by
mediating a pinned compiler or language server over the concrete workspace and
recording the resulting facts as auditable JSON/JSONL artifacts.

The system provides:

- **Robust selectors** beyond brittle `file:line:col`: cursor and range
  selectors, symbolic selectors, AST paths, content anchors, and explicit
  `utf-16`, `utf-8`, or codepoint indexing.
- **Deterministic analysis bundles** that normalize compiler/LSP responses,
  capture environment and capability metadata, and compute stable content
  hashes for replay and CI auditing.
- **Preview-first guarded mutations** for operations such as rename, with
  prepare checks, unified diffs, workspace jails, dirty-worktree guards,
  conflict reporting, and recoverable apply flows.
- **RLCSF process rewards** derived from diagnostic deltas, selector-confidence
  changes, safety-readiness changes, and structured tool-error penalties.

## CLI Overview

- `lanser def`, `lanser references`, `lanser hover`, `lanser symbols`, and
  `lanser diagnostics` query Pyright-backed language-server facts and emit
  deterministic analysis bundles with selector-resolution and cache metadata.
- `lanser rename` is preview-first: it checks rename readiness, emits the
  workspace edit and unified diff, and writes only when `--apply` is requested.
- `lanser batch` consumes JSONL command queues and emits JSONL bundles so
  planners can pipeline definition, reference, diagnostics, hover, symbols, and
  rename requests.
- Pass `--trace-file <path>` to capture orchestrator metadata and JSON-RPC
  traffic. `lanser trace replay` regenerates recorded operation outputs from
  the trace log for replay and regression tests.
- `lanser trace list` and `lanser trace show` expose recorded operations with
  filtering by operation, selector text, exit code, or status.
- `lanser schema validate`, `lanser schema validate-batch`, and `lanser schema
  export` publish and enforce JSON contracts for selectors, bundles, and
  historical fixtures.
- `lanser doctor` reports environment metadata, including the Python runtime,
  Pyright version, project files, configuration digest, Git state, and workspace
  snapshot.

## Selector DSL

Lanser selectors are designed to survive edits and surface ambiguity rather
than silently targeting stale coordinates. The selector forms include:

```text
src/app.py@L42:C7
src/app.py@R(42,7->44,1)
py://pkg.mod#Class.method:body
py://pkg.mod#function_name:sig
ast://[module=pkg.mod]/[class=Class]/[def=method]/name[1]
anchor://src/app.py#"def load_data("?ctx=24
```

Coordinate selectors carry explicit indexing semantics. Lanser negotiates the
server-side position encoding, records it in bundle metadata, and can expose
both server and CLI coordinate systems when needed.

## Analysis Bundles and Replay

Every operation can emit an analysis bundle containing the request, resolved
selector, language-server facts, edit preview, environment metadata,
capabilities, stable sorting keys, and optional process-reward information.
Bundle identifiers are SHA-256 hashes over a canonicalized, non-volatile subset
of the payload, excluding timestamps and run-local trace spans.

Under a frozen workspace snapshot, pinned tool version, fixed configuration,
and deterministic analyzer semantics, replayed bundles have byte-stable
hash-domain contents. This makes Lanser suitable for CI gates, offline
evaluation, process-supervision datasets, and counterfactual analysis of agent
policies.

## Guardrails for Mutation

Mutating operations fail closed. Lanser applies workspace edits only after
preflight validation, path-jail checks, allow/deny path filtering, clean
worktree enforcement unless `--allow-dirty` is provided, conflict detection,
and staged application. Dry-run previews remain the default, so agents can
inspect diffs and ambiguity evidence before committing changes.

## RLCSF Reward

For adjacent bundles, RLCSF computes a process reward from machine-checked
program facts. In the common undiscounted form, the reward credits reductions
in diagnostics, improvements in safety readiness, and improvements in selector
confidence, while penalizing structured tool errors:

```text
r_t = w_D (D_{t-1} - D_t)
    + w_S (S_t - S_{t-1})
    + w_A (A_t - A_{t-1})
    - w_E E_t
```

The reward is intended for online planning, reinforcement learning, offline
process supervision, and replayable evaluation. It is shaping signal rather
than a replacement for terminal task success.

## Tooling

- Pyright `1.1.407` is the primary language server version for development and
  CI, with compatibility retained for `1.1.406`.
- Supported Pyright versions are centralised in `lanser.pyright_version` and
  surfaced by `lanser doctor`.
- The orchestrator records environment metadata such as tool version, server
  version, negotiated position encoding, Python executable and version,
  configuration digest, platform, and workspace snapshot.

## Citation

```bibtex
@article{zhang2025rlcsf,
  title   = {Reinforcement Learning from Compiler and Language Server Feedback},
  author  = {Zhang, Yifan},
  journal = {arXiv preprint arXiv:2510.22907},
  year    = {2025},
}
```
