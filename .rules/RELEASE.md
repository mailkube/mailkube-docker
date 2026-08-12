# Release & Publishing

Load this when touching `.releaserc.json`, the release or publish workflows, image tags, cosign
signing, or the Docker Hub description sync.

## The contract

1. **Conventional Commits drive the version.** On push to `main`, `semantic-release` reads the commit
   history since the last tag: `fix:` -> patch, `feat:` -> minor, `feat!:` or a `BREAKING CHANGE:`
   footer -> major. `perf:` also releases. Anything else (`chore`, `docs`, `ci`, `refactor`, `test`)
   does **not** release. PRs are squash-merged on the **PR title**, so the PR title is the commit that
   semantic-release reads; the `pr-title` workflow enforces it.
2. **semantic-release regenerates `CHANGELOG.md`, commits it, tags `vX.Y.Z`, and creates a GitHub
   Release.** `tagFormat` is `v${version}` and the release branch is `main` (`.releaserc.json`).
3. **The tag is the trigger for publishing**, subject to the GITHUB_TOKEN limitation below.
4. **The image is signed with cosign, keyless**, using GitHub's OIDC identity. No key material is
   stored anywhere.
5. **`docs/DOCKERHUB.md` is pushed to the Docker Hub repository description** as the last step, so the
   image page never describes a version that is not published yet.

## Why a GITHUB_TOKEN-pushed tag cannot trigger a separate workflow

This is a GitHub Actions rule, not a misconfiguration and not something to debug:

> Events raised by the built-in `GITHUB_TOKEN` do **not** trigger further workflow runs.

So `semantic-release` pushing `v1.4.0` raises a `push` tag event that **no** `on: push: tags:` workflow
will ever see. Wiring the publish job to a tag trigger produces a release with no image, silently, and
only on the real release path where nobody is watching.

**The workaround in use: publish in the same job run, and read the version with `git describe`.**
After the semantic-release step, the workflow resolves the version from the repository state rather
than from a tag event:

```bash
# after semantic-release has run in this same job
version="$(git describe --tags --abbrev=0 2>/dev/null || echo '')"
if [ -z "$version" ]; then
  echo "no tag yet: nothing to publish"   # zero-tag guard, see below
  exit 0
fi
```

**The zero-tag guard is mandatory.** On a brand-new repository, and on any run where semantic-release
decided not to cut a release (a `docs:`-only merge, for example), there is no tag to describe.
`git describe --tags` **fails** in that situation rather than returning empty, so an unguarded
invocation aborts the job under `set -e` and reports a red build for a perfectly correct no-release
outcome. Guard it, and treat "no tag" as a successful no-op.

`fetch-depth: 0` is required on the checkout, or `git describe` cannot see any tag at all.

## Required permissions

```yaml
permissions:
  contents: write      # semantic-release commits CHANGELOG.md and pushes the tag
  issues: write        # semantic-release comments on released issues/PRs
  pull-requests: write
  id-token: write      # REQUIRED for keyless cosign signing
```

**`id-token: write` is not optional.** Keyless cosign obtains a short-lived certificate from Fulcio by
presenting a GitHub OIDC token; without this permission the token cannot be minted and `cosign sign`
fails at the end of an otherwise successful publish, leaving an unsigned image on Docker Hub. It must
be granted on the job that signs, and GitHub does not inherit it from a workflow-level default if the
job narrows permissions.

## Docker Hub credentials

- The registry login uses a **Docker Hub Personal Access Token** with scope **`read`, `write`,
  `delete`**. `read`+`write` alone is not enough for the tag hygiene the publish step performs.
- **The description sync additionally requires an account with the `Admin` permission on the
  repository.** The Docker Hub API endpoint that updates the repository description (`full_description`)
  rejects a token whose account only has write access, with a 403 that does not name the missing
  permission. If the description silently stops updating after a credential rotation, this is the
  cause.
- Both are repository secrets. Never inline a token into a workflow file, and never echo one.

## Image tag policy

For a release of version `X.Y.Z`, the manifest is pushed as:

| Tag | Meaning |
|---|---|
| `X.Y.Z` | the immutable release. Never re-pushed |
| `X.Y` | latest patch of that minor |
| `X` | latest minor of that major |
| `latest` | latest stable release |
| `edge` | latest build of `main`, published on every merge, not a release |

`edge` is deliberately outside the semver ladder: it exists so a change can be smoke-tested in a real
cluster before it is released, and nothing that pins a version tag is affected by it. Documentation
must tell users to pin at least `X.Y`, and to pin `X.Y.Z` in production.

Every tag is a multi-arch manifest covering `linux/amd64` and `linux/arm64`; see
`.rules/CONTAINER.md`.

## Do not

- Do not hand-edit `CHANGELOG.md` or any version string. semantic-release owns them.
- Do not add an `on: push: tags:` publish workflow. It will never fire; see above.
- Do not remove the zero-tag guard around `git describe`, and do not drop `fetch-depth: 0`.
- Do not drop `id-token: write` while "simplifying" permissions. The failure is an unsigned image, at
  the very end of the run.
- Do not add a cosign key pair or a signing passphrase secret. Keyless is the point.
- Do not re-push an existing `X.Y.Z` tag. Cut a new patch instead; consumers and cosign attestations
  both assume immutability.
- Do not gate the release workflow on anything weaker than the full CI set (`test` + `dry` + `docs`),
  and do not rename those jobs: they are branch-protection required checks.
