# Hermes Adapter

This optional adapter is for projects that intentionally use Hermes Agent. It is not the identity of Objective Integrity Framework.

Hermes exposes project guidance, skills, and native hooks. This adapter binds the framework's objective ledger to that host without changing the core objective or evidence model:

- Bind the ledger to the stable session identity the host integration actually exposes to the runtime (for example, a session-id environment variable read through the explicit `host_binding` configuration). Never infer continuity from a title, model, working directory, or repository name.
- Configure the runtime with `host_binding` (`session_env`, `bindings_dir`) and, when a localized human filename is preferred, `projection_filename`. Both are plain configuration; no host-specific fork of the runtime or skills is required.
- Connect prompt capture and recovery injection only through the host's supported hook configuration. Configuration, trusted activation, delivered context and observed owner behavior are different boundaries; keep a manual owner-read fallback.
- Host-side exact-action screening for this host's tool payload families belongs to this adapter layer: document the payload shapes, keep the screen mechanical (shape, enums, identity), and never promote its pass into semantic safety, authority, or completion.

The default workflow remains one same-conversation primary owner with read-only independent review when needed. See the [generic adapter](../generic/system-developer-adapter.md) and [Runtime Reference](../../docs/runtime-reference.md) for the exact configuration keys and command contracts.
