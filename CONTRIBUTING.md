# Contributing to mailkube-docker

Thanks for helping improve **mailkube-docker**, the companion SMTP relay image
(`mailkube/smtp-relay`) for [mailkube](https://mailkube.com).
Contributions of all kinds are welcome: bug reports, fixes, docs, and features.

By contributing you agree that your contributions are licensed under the project's
[Apache License 2.0](LICENSE) (inbound = outbound). **No CLA and no sign-off are required.**
Please also read our [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

Requires Docker (with buildx), Python 3.13 for the test harness, and Node.js for the `jscpd`
duplication check. `hadolint`, `shellcheck`, `shfmt` and `yamllint` are needed for the lint gates.

```bash
git clone https://github.com/mailkube/mailkube-docker
cd mailkube-docker

# 1. Build the image. This also runs the 7 build-time assertions.
docker build -t mailkube/smtp-relay:dev .

# 2. Build the test-only SMTP sink that stands in for smtp.mailkube.com.
docker build -t mailkube-relay-sink:dev test/sink

# 3. Generate the throwaway CA + leaf certificate for smtp.mailkube.com.
docker run --rm -v /tmp/mkrelay/certs:/certs \
  --entrypoint python mailkube-relay-sink:dev /opt/gen_ca.py /certs

# 4. Create the network and start the sink under the hardcoded upstream name.
docker network create mknet
docker run -d --name sink --network mknet --network-alias smtp.mailkube.com \
  -v /tmp/mkrelay/certs:/certs -v /tmp/mkrelay/out:/out mailkube-relay-sink:dev

# 5. Run the relay against it. The CA is installed as an ordinary hashed trust-store
#    entry, not as an override, and RELAY_START_JITTER=0 removes the startup delay.
docker run --rm --network mknet \
  -e SMTP_USERNAME=myapp01@example.com -e SMTP_PASSWORD=secret \
  -e RELAY_START_JITTER=0 \
  -v "/tmp/mkrelay/certs/ca.pem:/etc/ssl/certs/$(cat /tmp/mkrelay/certs/ca_hash.txt).0:ro" \
  mailkube/smtp-relay:dev
```

The full suite runs the same harness through pytest: `python3 -m pytest test/`.
See [.rules/TESTING.md](.rules/TESTING.md) for the two test seams, why the connection-reuse test uses a
spaced cadence, and why every environment variable needs a matrix row.

## Quality gates

Every change must pass the same checks CI runs (see
[.rules/SOLID_DRY_KISS.md](.rules/SOLID_DRY_KISS.md)):

```bash
hadolint Dockerfile                                    # Dockerfile lint (.hadolint.yaml, DL3018 waived)
shellcheck --shell=dash --external-sources \
  rootfs/usr/local/bin/*.sh \
  rootfs/usr/local/lib/mailkube-relay/*.sh             # dash dialect is load-bearing, see .shellcheckrc
shfmt -d -i 2 -ci -sr rootfs/                          # shell formatting (-d = diff, fails on drift)
yamllint examples/ .github/                            # shipped manifests + workflows
docker build -t mailkube/smtp-relay:dev .              # runs the 7 build-time assertions
python3 -m pytest test/                                # integration matrix against test/sink
npx --yes jscpd@4 --config .jscpd.json .               # duplication (DRY) gate, blocks at > 1%
./scripts/check-rule-index.sh                          # every .rules/*.md indexed in AGENTS.md
./scripts/check-env-matrix.sh                          # every env var + validation branch has a test row
```

Two of these are unusual and are not negotiable:

- **`--shell=dash` on shellcheck.** It is what suppresses `SC3040` for the `set -o pipefail` that
  BusyBox ash genuinely supports. Removing it creates the finding it looks like it is hiding.
- **`docker build` counts as a test.** Three assumptions that cannot be checked any other way (Alpine's
  package layout, Postfix's compiled-in map types, the services `master.cf` references) are asserted
  there and fail the build.

## Commit & PR conventions

This project follows **[Conventional Commits](https://www.conventionalcommits.org/)**. A CI check
enforces the **PR title** (PRs are **squash-merged** using it), and it drives releases: only
`feat:`, `fix:`, and `perf:` cut a new version. See [.rules/RELEASE.md](.rules/RELEASE.md).

Suggested scopes: `postfix`, `entrypoint`, `image`, `test`, `ci`, `docs`, `examples`.

```
feat(entrypoint): add RELAY_NETWORKS_MODE=replace
fix(postfix): set connection_cache_ttl_limit so the 45s cache is not capped to 2s
docs: document the fleet connection budget
```

Before opening a PR, read the rule file that covers what you touched. Most values in this repo defend a
specific verified upstream limit, and the failure mode for getting one wrong is usually silent locally
and expensive in production (see [.rules/UPSTREAM_CONTRACT.md](.rules/UPSTREAM_CONTRACT.md)). A PR that
changes a tuning value should say which limit it defends and include the arithmetic.

## Reporting bugs / requesting features

Open an issue using the templates. For **security vulnerabilities**, do not open a public
issue; follow [SECURITY.md](SECURITY.md) instead.
