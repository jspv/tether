# Roadmap

Status of work beyond the shipped v0.1.1 surface. The README documents what exists;
this file tracks what doesn't yet. Per-item design specs and phased plans live in
`docs/superpowers/specs/` and `docs/superpowers/plans/`.

Last reviewed: 2026-09-17.

## In progress

### Security hardening — make the trust boundary real
**Priority: next.** The threat model (below) says the model and everything it fetches are
untrusted, but the code does not yet enforce that. Four workstreams:

1. **Untrusted sandbox control plane.** The parent trusts child-written handle metadata
   (`preview`, `bytes`, `schema`, `n_rows`) verbatim and re-enters it into model context,
   uncapped; `HandleStore.register` also lets a child overwrite the record of a handle id it
   did not create. Paths are already validated through `safe_path`, so this is a metadata
   integrity and context-flood problem, not a path-escape one.
2. **Isolation off by default.** `SandboxConfig.backend` defaults to `"local"` — a scrubbed
   subprocess running as the host user. The container tier exists and is hardened; it should
   be the default, with `local` an explicit, loudly-named opt-out.
3. **No SSRF filtering on egress.** `fetch_url` runs parent-side with no private/loopback/
   link-local/metadata-IP blocking and no redirect re-validation. `web_extract` and
   `read_document(url)` share the exposure and need the same guard.
4. **Cross-conversation resource sharing.** `SessionManager.aopen` passes the same live
   `_tether._tools` list (including connected MCP servers) to every conversation, making it a
   cross-tenant leak vector rather than just a lifecycle wart.

## Planned

### Micro-VM sandbox tier
gVisor / Firecracker behind the existing `SandboxExecutor` interface, as a third `backend`
value alongside `local` and `container`. Deliberately sequenced after the hardening work:
adding a stronger tier while the default tier provides no isolation would not move the
weakest link.

### MAF skills + memory providers (v1.1)
Wire MAF's `Skill`/`SkillsProvider` and `MemoryStore`/`MemoryContextProvider` into the
harness so the agent can accumulate reusable procedures and cross-session memory. Both are
marked experimental upstream and emit `ExperimentalWarning`, so pin behavior carefully.

### Workflow durability / HITL outer shell
Wrap the agent loop in a MAF Workflow to get checkpointing, resume, and human-gated steps —
the durable outer orchestration that the original research reserved Workflows for, with the
single agent loop as a node inside it.

## Smaller carry-overs

Real but low-severity; each is a contained fix.

- **`read_file` has no binary-file handling** (`tools/files.py`) — a binary file decodes to
  replacement characters instead of returning a clear "this is binary, use a handle" signal.
- **`inspect_handle` calls `describe()` unguarded** (`tools/inspect.py:18`) — the shape is
  wrong for non-numeric frames, and it materializes the whole object to do it.
- **`max_file_size_mb` is silently ignored on the container tier** (documented in the README,
  but a config field that does nothing on the default-to-be backend is a trap).
- **`_OrchestratedSandbox` is sequential and not re-entrant per root** — fine at current
  scale; revisit if concurrent runs per conversation are ever needed.
- **Handle preview parity is hand-maintained.** `runtime/tether_sandbox.save` must mirror
  `HandleStore._write_dataframe` exactly because the child cannot import the package. A
  parity test guards it; re-deriving metadata parent-side (hardening item 1) would retire
  the duplication.

## Explicitly out of scope

Decided, not deferred — revisit only with a concrete need.

- Headless-browser / JS rendering for fetches.
- A full search-provider abstraction (Tavily is the one provider).
- Alternative document backends (Docling is the one backend).

## Threat model

Decided 2026-06-15. **The model itself, and any content it pulls from the web or documents,
is treated as potentially adversarial.** Prompt-injection-in-fetched-content escalating to
code execution or SSRF is in scope. Consequences: the sandbox must be a real isolation
boundary, not a nominal one; egress is a model-controlled capability and must be filtered;
and per-conversation resources must not be shared. When touching sandbox, fetch, handles, or
conversation resource wiring, assume the model is an adversary whose capability set is the
tool surface.

Registry scope (in-process / single-event-loop vs. distributed) is **undecided**. Preserve
optionality: invest in the per-conversation lease, the clean async `ConversationStore` seam,
and bounded capacity — all valuable under either answer — and defer persistence and
distribution.
