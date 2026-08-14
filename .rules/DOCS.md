# Documentation Rules

Load this before editing `README.md` or `examples/**`.

## One README, two surfaces

| File | Audience | Rendered by |
|---|---|---|
| `README.md` | the repository on GitHub, **and** the GHCR package page | GitHub Flavored Markdown |
| `examples/**` | someone copy-pasting a working deployment | not rendered, executed |

`README.md` is the full reference: architecture, the environment variable table, the security model,
troubleshooting, alert expressions, and the fleet connection budget from
`.rules/POSTFIX_TUNING.md`.

There is deliberately **no second, condensed registry document**. GHCR renders this same `README.md`
on the package page, through the repository link that `org.opencontainers.image.source` establishes,
so a registry-specific copy would be a file to keep in sync with no reader of its own. The Docker Hub
era needed one because that description had to be pushed; this one does not. Do not reintroduce it.

Two consequences follow, and both are improvements over the previous rule set:

- **Mermaid is fine everywhere.** GitHub renders it on both surfaces. Docker Hub did not, which is
  why the old rule banned it from the registry document.
- **Relative links are fine.** GitHub resolves them on the package page as it does in the repository.

## Duplication (jscpd) and the exemption decision

`.jscpd.json` blocks at > 1% duplicated code with `minTokens: 50`, and **Markdown is in scope**. Two
exemptions exist and are deliberate:

- `**/CHANGELOG.md`: inert here, since this repo has no changelog file (`.rules/RELEASE.md` explains
  why). Kept so the ignore list stays identical to the other mailkube repos, where semantic-release
  does generate one and repeated release stanzas are duplication by construction.
- `**/examples/**`: the Compose, Kubernetes and OpenShift manifests are necessarily near-identical
  (same environment block, same probes, same securityContext). Factoring them apart would defeat the
  entire purpose of an example, which is to be copy-pasted whole. This is the one place where
  duplication is the feature.

**`**/*.md` must never be added to the ignore list.** It would exempt the whole `.rules/` corpus, which
is exactly where copy-paste between rule files does real damage. If a Markdown file trips the gate,
shorten it and link out.

## What the `docs` CI job checks

- `scripts/check-rule-index.sh`: every `.rules/*.md` file has a row in the `AGENTS.md` index table. An
  unindexed rule is invisible to progressive disclosure, which is the whole reason the index exists.
- `scripts/check-env-matrix.sh`: every variable in `env.sh` appears in `.env.example`, `README.md`,
  and at least one test.
- Markdown lint and link checking over the tracked Markdown files.
- `yamllint` over `examples/` and `.github/`, so a shipped manifest cannot be syntactically broken.

The job name `docs` is a branch-protection required check. Never rename it; see `AGENTS.md`.

## Rules for the content itself

- **Every number in the docs must match the code.** The environment variable table's defaults come from
  `rootfs/usr/local/lib/mailkube-relay/env.sh`; the limits come from `main.cf`. A docs change that
  contradicts the source is a bug, not a wording preference.
- **State the fleet rule wherever concurrency appears**: `instances × RELAY_CONCURRENCY ≤ 9`, because
  the container cannot see the instance count and cannot validate it. Say why the budget is halved:
  each unit of concurrency holds two upstream connections, one delivering and one cached idle for
  reuse, and an idle connection still occupies a slot.
- **State the security model plainly**: the ingress listener is unauthenticated by design; `mynetworks`
  is the second line of defence and network isolation is the first. Point at
  `examples/kubernetes/30-networkpolicy.yaml`.
- **Troubleshooting entries must quote the exact log line** they key on. Those shapes are a contract
  with `log.sh` and with the alert expressions; if you reword one, update both in the same PR.
- Do not document an environment variable that does not exist in `env.sh`, and do not omit one that
  does. `scripts/check-env-matrix.sh` covers tests, not prose, so this one is on review.
