# Documentation Rules

Load this before editing `README.md`, `docs/DOCKERHUB.md`, or `examples/**`.

## The two-audience split

| File | Audience | Rendered by |
|---|---|---|
| `README.md` | someone reading the repository on GitHub | GitHub Flavored Markdown |
| `docs/DOCKERHUB.md` | someone reading the image page on Docker Hub | Docker Hub's Markdown renderer |
| `examples/**` | someone copy-pasting a working deployment | not rendered, executed |

`README.md` is the full reference: architecture, the environment variable table, the security model,
troubleshooting, alert expressions, and the fleet connection budget from
`.rules/POSTFIX_TUNING.md`.

`docs/DOCKERHUB.md` is a **condensation**, not a copy. It is pushed to the Docker Hub repository
description by the release workflow (see `.rules/RELEASE.md`), and it exists to get a first-time user
to a working `docker run` and then send them to GitHub. Keep it to: what the image is, the minimum
run command, the environment variable table, the ports, the tags, and links.

## Mermaid policy

**Docker Hub does not render mermaid.** A ```` ```mermaid ```` fence appears there as a wall of raw
diagram source, which is worse than no diagram.

- `README.md` may use mermaid freely, and does (architecture and message-flow diagrams).
- `docs/DOCKERHUB.md` must contain **no mermaid at all**. If a concept needs a picture there, link to
  the README section that has it.

## Absolute URLs in `docs/DOCKERHUB.md`

Docker Hub serves the description outside the repository, so a relative link (`[example](examples/kubernetes/)`,
`[see the rules](.rules/CONTAINER.md)`) resolves against `hub.docker.com` and 404s.

> **Every link and every image reference in `docs/DOCKERHUB.md` must be an absolute
> `https://github.com/mailkube/mailkube-docker/...` URL.** Badges must point at absolute raw URLs too.

`README.md` uses relative links normally, because GitHub resolves them.

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

**`docs/DOCKERHUB.md` is NOT exempt, and must not be added to the ignore list.** That is the enforcement
mechanism for the condensation rule above: if it drifts into being a copy of `README.md`, the `dry` job
fails, which is the intended signal. Fix it by shortening `DOCKERHUB.md` and linking out, never by
widening the ignore list.

## What the `docs` CI job checks

- `scripts/check-rule-index.sh`: every `.rules/*.md` file has a row in the `AGENTS.md` index table. An
  unindexed rule is invisible to progressive disclosure, which is the whole reason the index exists.
- Markdown lint and link checking over the tracked Markdown files.
- `yamllint` over `examples/` and `.github/`, so a shipped manifest cannot be syntactically broken.

The job name `docs` is a branch-protection required check. Never rename it; see `AGENTS.md`.

## Rules for the content itself

- **Every number in the docs must match the code.** The environment variable table's defaults come from
  `rootfs/usr/local/lib/mailkube-relay/env.sh`; the limits come from `main.cf`. A docs change that
  contradicts the source is a bug, not a wording preference.
- **State the fleet rule wherever concurrency appears**: `instances × RELAY_CONCURRENCY ≤ 18`, because
  the container cannot see the instance count and cannot validate it.
- **State the security model plainly**: the ingress listener is unauthenticated by design; `mynetworks`
  is the second line of defence and network isolation is the first. Point at
  `examples/kubernetes/30-networkpolicy.yaml`.
- **Troubleshooting entries must quote the exact log line** they key on. Those shapes are a contract
  with `log.sh` and with the alert expressions; if you reword one, update both in the same PR.
- Do not document an environment variable that does not exist in `env.sh`, and do not omit one that
  does. `scripts/check-env-matrix.sh` covers tests, not prose, so this one is on review.
