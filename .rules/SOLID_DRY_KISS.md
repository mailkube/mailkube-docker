# Engineering Standards: SOLID · DRY · KISS · Behavior Coverage · Docs

These are **enforced by CI**. A PR that violates them cannot merge. This file gives the exact
thresholds and how to satisfy each gate locally *before* pushing.

## The five pillars

| Pillar | Rule | Enforced by |
|---|---|---|
| **Behavior coverage** | every environment variable and every validation branch has a named test row | `scripts/check-env-matrix.sh` (the `test` CI job) |
| **DRY** | <= 1% duplicated code | `jscpd` (the `dry` CI job), `.jscpd.json` |
| **KISS** | one responsibility per shell library; no function that needs "and" to describe it | review + the file layout below |
| **Documentation** | every non-obvious value carries the reason it holds, at the value | review + the `docs` CI job |
| **SOLID** | see below, approximated by the library split + review | `shellcheck`, `shfmt`, `hadolint`, PR checklist |

### Coverage is redefined here, deliberately

The house pillar in every other mailkube repo is **>= 90% line and branch coverage**. This repo has no
application code to line-cover: it is a `Dockerfile`, a Postfix configuration, and roughly 700 lines of
POSIX `sh` whose entire job is to translate environment variables into `postconf -e` arguments and then
`exec` Postfix. A line-coverage number over that surface would be trivially gamed by a single
"container boots" test and would say nothing about correctness.

**The signed-off deviation: coverage here means behavior coverage.** Every environment variable
consumed in `rootfs/usr/local/lib/mailkube-relay/env.sh` and every rejection branch in `validate.sh`
MUST have a named row in the test matrix (`.rules/TESTING.md`), asserting both the accepting and the
rejecting side where a rejecting side exists. `scripts/check-env-matrix.sh` is the gate: it fails the
`test` job when a variable exists in `env.sh` or a `die_hint` exists in `validate.sh` with no matching
matrix row. Adding a variable without a row is the same class of failure as dropping the line-coverage
gate in the other repos, and is refused for the same reason.

This deviation applies to this repository only. Do not cite it in a repo that has application code.

## Run the gates locally

```bash
hadolint Dockerfile                                    # Dockerfile lint (.hadolint.yaml, DL3018 waived)
shellcheck --shell=dash --external-sources \
  rootfs/usr/local/bin/*.sh rootfs/usr/local/lib/mailkube-relay/*.sh   # dash dialect is load-bearing
shfmt -d -i 2 -ci -sr rootfs/                          # formatting (-d = diff, non-zero on drift)
yamllint examples/ .github/                            # manifests and workflows
docker build -t mailkube/smtp-relay:dev .              # runs the 7 build assertions
python3 -m pytest test/                                # integration matrix against test/sink
./scripts/check-env-matrix.sh                          # every env var + validation branch has a row
npx --yes jscpd@4 --config .jscpd.json .               # duplication (DRY) gate, blocks at > 1%
./scripts/check-rule-index.sh                          # every .rules/*.md indexed in AGENTS.md
```

`docker build` is part of the gate set, not a convenience: three assumptions that cannot be tested any
other way (Alpine's package layout, Postfix's compiled-in map types, the services `master.cf`
references) are asserted in the `Dockerfile` and fail the build. See `.rules/CONTAINER.md`.

## SOLID, concretely, for a shell and config repo

- **S**ingle responsibility. One concern per library under
  `rootfs/usr/local/lib/mailkube-relay/`: `log.sh` formats, `env.sh` loads and derives, `validate.sh`
  rejects, `render.sh` writes configuration and maps, `lifecycle.sh` gates the boot and supervises.
  `docker-entrypoint.sh` is orchestration only and contains no logic of its own. A new concern is a
  new file, not a new branch inside `render.sh`.
- **O**pen/closed. Add a new tuning value by adding a line to `main.cf` with its rationale, or a new
  argument to the single `postconf -e` batch in `apply_config()`. Do not add a second `postconf` call
  and do not append to `main.cf`; see `.rules/ENTRYPOINT.md`.
- **L**iskov. Every `assert_*` helper in `validate.sh` has the same contract: it returns silently on
  success and calls `die_hint` with a message plus an actionable fix on failure. A new assertion that
  warns instead of dying, or dies without a fix line, breaks that contract.
- **I**nterface segregation. The container's public interface is the environment variable set plus the
  listener port. Nothing else is contract. In particular there is no `POSTFIX_EXTRA_CONF` escape hatch,
  which was rejected: it is an arbitrary-parameter injection surface into `main.cf` that no allowlist
  can bound.
- **D**ependency inversion. Tests depend on abstractions the product already has (DNS resolution and
  the OpenSSL trust store), never on test-only code paths inside the image. See `.rules/TESTING.md`.

## KISS, concretely

The image is a null client. No local delivery, no virtual maps, no LDAP or SQL, no chroot, single build
stage, no CMD. Every one of those is an explicit decision recorded in the `Dockerfile` and `master.cf`
headers. Reintroducing any of them needs a stated reason in the PR.

## Requesting a waiver

A genuinely wrong threshold on a specific line gets a **scoped, commented** ignore that names the
reason, in the style already used in the tree (`# shellcheck disable=SC3040` above the guarded
`set -o pipefail`, `# hadolint ignore=DL3018` above the `apk add`). Blanket relaxations (removing a
gate from CI, deleting a matrix row, raising the jscpd threshold) require maintainer sign-off in the PR.
