# Project Rules

`mailkube-docker` is a public (Apache-2.0) mailkube companion SMTP relay, published to Docker Hub as
`mailkube/smtp-relay`. Applications in a cluster send plain, unauthenticated SMTP to it on port 25; it
relays to `smtp.mailkube.com` over verified TLS with AUTH PLAIN, giving those applications a durable
local async queue and keeping the SMTP credential in exactly one place. Load the relevant rule file
from `.rules/` based on the task.

## Rule Index

> **Index every rule (required).** Every file in `.rules/` MUST have a row in the table below. When you
> add or rename a `.rules/` file, add or update its row in the **same change**. An unindexed rule is
> invisible, because this index is what drives progressive disclosure. The `docs` CI job
> (`scripts/check-rule-index.sh`) fails the build if `.rules/` and this index drift. This convention
> holds for every mailkube repo.

| Rule File | Load When |
|---|---|
| `.rules/SOLID_DRY_KISS.md` | Writing or changing anything in this repo: the enforced engineering standards (SOLID, DRY, KISS, behavior coverage, docs) and how to run each gate locally. |
| `.rules/UPSTREAM_CONTRACT.md` | Touching anything that talks to `smtp.mailkube.com`: the relay host, the port, AUTH, the username shape, the From rule, the `X-Mailkube-*` headers, or the 4xx-to-risk-signal-to-IP-ban chain. Read before "improving" any of them. |
| `.rules/POSTFIX_TUNING.md` | Editing `rootfs/etc/postfix/main.cf` or `master.cf`, or any tuning value the entrypoint applies: connection reuse, concurrency, timeouts, backoff, size and rate limits. |
| `.rules/CONTAINER.md` | Editing the `Dockerfile`, the base image pin, the package list, the build assertions, OCI labels, the healthcheck, or the capability / `readOnlyRootFilesystem` posture. |
| `.rules/ENTRYPOINT.md` | Editing `rootfs/usr/local/bin/*.sh` or `rootfs/usr/local/lib/mailkube-relay/*.sh`: shell style, the config-apply contract, validation, secret handling, signals and drain. |
| `.rules/TESTING.md` | Adding or changing tests, the `test/sink` harness, the test seams, or any environment variable (every variable needs a matrix row). |
| `.rules/DOCS.md` | Editing `README.md`, `docs/DOCKERHUB.md`, `examples/**`, or any mermaid diagram. |
| `.rules/RELEASE.md` | Touching `.releaserc.json`, the release / publish workflows, image tags, cosign signing, or the Docker Hub description sync. |

## Key Conventions (always apply)

- **POSIX `sh` only** (BusyBox ash at runtime). No bashisms: no arrays, no `[[`, no `local`, no
  `${var,,}`. `set -o pipefail` is the one allowed exception and is guarded, see `.rules/ENTRYPOINT.md`.
- **ShellCheck with `--shell=dash`, scoped to `rootfs/**`.** The dialect is load-bearing, not cosmetic:
  ShellCheck suppresses `SC3040` ("`set -o pipefail` is undefined") only for dash, and BusyBox ash
  really does support pipefail. Dropping `--shell=dash` lets shebang detection pick `sh` and *creates*
  the finding. Pinned in `.shellcheckrc`.
- **`shfmt -i 2 -ci -sr`** formats every shell file. **hadolint** lints the `Dockerfile`
  (`failure-threshold: warning`, `.hadolint.yaml`).
- **The base image is pinned by digest** (`alpine:3.24@sha256:28bd…3f8b`). Dependabot bumps it; never
  replace the digest pin with a floating tag.
- **No secrets in the image or the repo.** The SMTP credential arrives at runtime through
  `SMTP_PASSWORD` / `SMTP_PASSWORD_FILE` and is written to `/run/postfix/sasl_passwd` on tmpfs.
- **CI job names `test`, `dry` and `docs` are branch-protection required checks.** Renaming a job
  silently removes its protection. Never rename them; add new jobs instead.
- **Conventional Commits** for PR titles (PRs are **squash-merged** on the title); only `feat:`,
  `fix:` and `perf:` cut a release.
- **Never hand-edit `CHANGELOG.md` or a version string.** semantic-release owns both.
