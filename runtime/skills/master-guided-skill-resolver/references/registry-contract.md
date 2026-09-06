# Registry contract v1

The registry path supplied explicitly to each command is a compact executable projection of project knowledge. The workflow and exact current records remain authoritative.

Each entry declares a stable skill ID, skill name, semantic version, origin, relative discovery path, status, disclosure class, trigger clauses, authority and resource requirements, expected delta, proof ceiling, cost, expiry/revalidation, rollback, master links, and an exact file manifest. All active matching entries are selected; array order is not priority.

Origin roots are resolved at runtime. `user` means the explicitly supplied user root; `project` means the explicitly supplied repository discovery root. Preserve both absolute lexical and resolved physical identities: inspect every lexical root-to-skill component before resolution, disclose each reparse component with its resolved target, bind that ordered list into the receipt/snapshot, and require the final physical path to remain under the resolved origin root. An escape is rejected; a later retarget changes the snapshot and is stale/conflicting with an expected snapshot.

Status values are `candidate`, `shadow`, `audited`, `active-bounded`, `measured`, `superseded`, and `retired`. Only `active-bounded` and `measured` entries are selectable. A superseded or retired skill must also be outside every active discovery root; registry status alone is insufficient retirement. Such an entry must bind an existing absolute `evidence_path` outside all active roots, an exact `evidence_files` manifest, and lineage containing `reason`, `effective_utc`, and `prior_status`. Supersession additionally records `lineage.replacement` with `skill_id`, `version`, and `origin`, and that tuple must resolve to exactly one registry entry.

The registry file is excluded from its own manifest to avoid a self-hash cycle. Its raw SHA-256 is always included separately in the selection receipt. Every other regular file under a registered skill directory must be represented by the manifest unless explicitly declared `evidence_only` outside discovery roots.

Registry validation is structural. Semantic applicability, objective fidelity, blind completeness, consumer safety, and empirical benefit remain under workflow evidence routes.
