# Entrypoint and Shell Rules

Load this before editing `rootfs/usr/local/bin/*.sh` or
`rootfs/usr/local/lib/mailkube-relay/*.sh`.

## Layout and responsibilities

`docker-entrypoint.sh` is **orchestration only**. It sources the libraries and calls `main()`, which
runs the steps in a fixed order. Several steps depend on values a previous one derived, so the order is
part of the contract:

```
load_env              # *_FILE indirection, defaults
validate_legacy_env   # BEFORE validate_env, so a renamed variable is named as such
derive_identity       # AUTH_DOMAIN, needed by BOUNCE_RECIPIENT validation
validate_env
build_networks
prepare_config_dir    # copy /etc/postfix -> /run/postfix
normalize_mounts
apply_config          # the single batched postconf -e
write_credentials
write_maps
preflight
warn_if_publicly_reachable
start_and_supervise
```

One concern per library: `log.sh` formats, `env.sh` loads and derives, `validate.sh` rejects,
`render.sh` writes configuration and maps, `lifecycle.sh` gates the boot and supervises. Loading is
separated from validation on purpose, so every value is present before any rule runs and an error
message can reference another variable (checking `BOUNCE_RECIPIENT` against `AUTH_DOMAIN`, for
example).

## POSIX sh style

The runtime shell is **BusyBox ash**. Write POSIX `sh`: no arrays, no `[[ ]]`, no `local`, no
`${var,,}`, no `+=`. POSIX sh has no arrays, so an argument list is built positionally with `set --`,
which also preserves values containing spaces correctly.

`set -eu` at the top of the entrypoint. `set -o pipefail` is the one exception and is written exactly
like this:

```sh
# shellcheck disable=SC3040
set -o pipefail 2>/dev/null || true
```

BusyBox ash supports pipefail; dash does not. The tree is linted `--shell=dash` because that dialect is
what suppresses SC3040 for everything else, so this one line carries a scoped disable and a `|| true`
guard. Do not fold it into the `set -eu` line and do not remove `--shell=dash` from `.shellcheckrc`.

Library files start with `# shellcheck shell=dash` rather than a shebang, because they are sourced.

Formatting is `shfmt -i 2 -ci -sr`. Run `shfmt -d -i 2 -ci -sr rootfs/` before pushing.

## The config-apply contract

> **Every environment-derived parameter is applied with ONE batched `postconf -e` call against
> `$MAIL_CONFIG`. Never append to `main.cf`.**

Postfix honours the *last* definition of a parameter, so an appended overlay "works". But
`dict_load_fp()` emits `overriding earlier entry: <name>=<value>` to stderr for **every** duplicate,
and those warnings land on exactly the parameters an overlay exists to override. `preflight()` treats
warnings of that class as fatal, so an overlay turns every configurable value into a boot failure.
`postconf -e` edits in place, preserves the rationale comments in `main.cf`, and produces no
duplicates.

Corollaries:

- Add a parameter to the existing `set --` list in `apply_config()`. Do not add a second `postconf`
  call, and do not write to `main.cf` with `cat >>` or `sed`.
- `_apply_listen_port()` is the one legitimate separate call, because `LISTEN_PORT` lives in
  `master.cf` and uses `postconf -M`/`-MX`, not `-e`. The port-25 entry is **removed** (`-MX`) and
  replaced, never supplemented.
- `_write_header_checks()` also calls `postconf -e` separately because whether `smtp_header_checks` is
  set at all depends on the generated file's contents. Keep that shape: it sets the parameter to a map
  path or to empty, never leaves a stale value.

## Validation is the grammar allowlist

`validate.sh` has two jobs. The second is the security-relevant one:

> Every value that reaches `postconf -e` must be matched by an explicit grammar rule first. Anything
> not matched does not reach Postfix.

Without that, the entrypoint is the same arbitrary-parameter injection surface into `main.cf` that
`POSTFIX_EXTRA_CONF` was rejected for. Adding a new environment variable that lands in a Postfix
parameter therefore requires an `assert_*` call in `validate_env()`, in the same change.

`assert_clean()` rejects newlines, carriage returns and `#`: a newline would append an arbitrary
parameter, a `#` would comment out the remainder of a generated line. It is implemented as
strip-and-compare, not as a `case` glob, for the reason in "Bugs never to reintroduce" below.

Note what preflight can and cannot do, so validation is not weakened on a false assumption.
`postfix check` **exits 0 even when it warns**: `postfix-script`'s `check)` branch ends in `exit 0`
after `check-warn`, so an exit-code assertion alone is vacuous, and the output predicate in
`preflight()` is what does the work. It catches misspelled parameter **names** and ownership or
permission problems. It does **not** catch bad **values**:
`postconf -e smtp_destination_concurrency_limit=not-a-number` passes silently. That is precisely why
the grammar allowlist exists, and why test T-12b is a positive control that the preflight predicate has
not silently become a no-op.

Every rejection branch must use `die_hint` with an actionable second line. A bare rejection sends
people to the source; a hint does not. Every rule needs a named row in the test matrix
(`.rules/TESTING.md`).

Legacy-variable handling is **tiered on purpose**. `SMTP_NETWORKS`, `SERVER_HOSTNAME`, `OVERWRITE_FROM`
and friends are fatal, because silently ignoring them is how a migrating user loses their entire
ingress allowlist at cutover. `SMTP_SERVER` and `DEBUG` only warn, because those are generic names that
legitimately belong to a co-located application when a Compose `env_file` is shared. Keep that
distinction when adding to the list.

## Secret handling

Four rules, all currently satisfied by `load_secret_file()` and `write_credentials()`:

1. **`set +x` around anything touching a credential**, with `_restore_trace` afterwards, so
   `RELAY_DEBUG=yes` can never echo a password into the log.
2. **A missing or unreadable `*_FILE` is FATAL, never skipped.** The lineage script printed "skipping"
   and carried on, turning a broken secret mount into a confusing 535 half a minute later.
3. **Trailing whitespace is trimmed.** `kubectl create secret --from-file` with a file made by `echo`
   (not `echo -n`) embeds a trailing newline, which is the single most common silent 535 in this
   ecosystem. The trim is `tr -d '\r' | sed -e 's/[[:space:]]*$//' | head -n 1`, deliberately not a
   bare `$(cat …)`: command substitution alone strips newlines but keeps other trailing whitespace.
4. **The credential lands only on tmpfs**, at `${MAIL_CONFIG}/sasl_passwd` (mode 0640, owned
   `root:mail_owner`), as a `texthash:` map so there is no `postmap` step and no `.lmdb` sidecar.

## Signals, drain, and the supervisor exit contract

`start_and_supervise()` deliberately does **not** `exec`, because a custom drain is needed on SIGTERM.

The `trap graceful_stop TERM INT` is mandatory, not stylistic: **PID 1 has no default signal
dispositions**, so an untrapped SIGTERM to a PID-1 shell is *ignored* and the container sits there until
SIGKILL. No init shim is needed: there is exactly one child, and `master` reaps its own children.

The exit contract:

| Situation | Exit code | Why |
|---|---|---|
| SIGTERM, queue drained inside the budget | `0` | clean shutdown |
| SIGTERM, queue **not** drained inside `SHUTDOWN_DRAIN_TIMEOUT` | `75` | EX_TEMPFAIL. Distinguishable from a crash, and it is a legitimate outcome: a sidecar is not guaranteed a drain window, because a pod has ONE `terminationGracePeriodSeconds` spent sequentially across containers |
| `master` exits on its own | its own status | always an error; logged as `postfix master exited unexpectedly with status N` |
| Configuration rejected at boot | `1` | via `die` / `die_hint` |

Before exiting 75 the supervisor prints a **fixed-shape** line that the README alert expressions match:

```
mailkube-relay: drain-incomplete undelivered=<n> timeout=<n>s
```

The `wait` loop guards on `$_STOPPING`, because POSIX `wait` returns `128+signum` when a trap fires and
by then `graceful_stop` has already run and exited. Reaching past the loop means `master` died on its
own.

Two more details that look removable and are not: `rm -f /var/spool/postfix/pid/master.pid` before
start, because a remounted spool carries a stale pid file from the previous container; and
`RELAY_START_JITTER`, which decorrelates synchronized multi-replica cold starts (set it to 0 in tests).

Postfix has no "stop accepting but keep delivering" mode, so a message can still arrive mid-drain. The
Kubernetes-side fix is a `preStop` hook that lets endpoint removal win the race; it lives in the
manifests, not here.

## Log message shapes are a contract

`log.sh` writes everything to **stderr**, so it can never be confused with Postfix's own stdout maillog
stream, which operators grep for delivery status lines. The message shapes are matched by the README
"Troubleshooting" section and by the alert expressions. Do not reword an existing line without updating
both in the same PR.

## Bugs never to reintroduce

Both were found during implementation.

### 1. The `$(printf '\n')` empty-glob trap

`assert_clean` originally built a `case` glob from `$(printf '\n')`. **Command substitution strips
trailing newlines**, so that expression is the empty string and the pattern `*""*` matches *every*
value, including an empty one. Three validation tests were passing for the wrong reason. Never build a
pattern from a command substitution that produces only whitespace; use strip-and-compare, as the
current code does.

### 2. The `$(...)` word-splitting trap in the config applier

`set -- "$@" $(_bounce_params)` split `k = v` into three arguments and Postfix rejected the batch with
`missing '=' after attribute name`. Unquoted command substitution word-splits on spaces, and every
`postconf -e` argument contains spaces by construction. Bounce parameters are now appended inline as
separate quoted arguments. Never feed a helper's stdout into `set --` unquoted.

## Inbound authentication steps

`collect_auth_accounts` runs before `apply_config`, because the configuration branches on whether
any account exists. `write_auth_db` runs after `prepare_config_dir`, because the database is written
into `$MAIL_CONFIG`.

**The SASL realm must be passed explicitly to `saslpasswd2`, and must equal
`smtpd_sasl_local_domain`.** Cyrus keys entries as `user@realm`. With no `-u`, `saslpasswd2` defaults
the realm to the machine hostname, which inside a container is a random ID that changes on every
start. The result is a database whose keys never match what Postfix looks up: `AUTH` is advertised,
every attempt returns 535, and because the database is rebuilt at boot it appears to work until the
first restart. Both sides are pinned to `$MYHOSTNAME`, which derives from the authenticated domain
and is therefore stable. Test T-27 restarts the container and re-authenticates specifically to hold
this.

The password database is rebuilt from scratch on every start, so removing an account from the
environment actually removes it.
