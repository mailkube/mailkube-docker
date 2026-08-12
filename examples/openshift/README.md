# Running mailkube/smtp-relay on OpenShift

## The limitation, stated plainly

**This image does not run under OpenShift's default `restricted-v2` SCC.** It
needs a custom SecurityContextConstraints, and that means a cluster
administrator has to grant it. There is no configuration flag, no build
argument, and no "unprivileged mode" that removes this requirement.

Two independent reasons, both structural:

1. **`restricted-v2` assigns a random UID** from the namespace's
   `openshift.io/sa.scc.uid-range` annotation. Postfix's `postfix-script`, which
   is what `postfix start` actually executes, checks the invoking user against
   the accounts it knows about and refuses to run under an arbitrary UID.
2. **`restricted-v2` forbids running as root.** Postfix's `master` daemon must
   start as root: it binds the listener, then drops privilege to the `postfix`
   user for every daemon it spawns. That privilege drop needs `SETUID` and
   `SETGID`, which `restricted-v2` also drops.

A container that starts as UID 1000730000 with no capabilities will fail during
startup, before it ever tries to send mail. Nothing in the entrypoint can paper
over it.

If your cluster policy does not permit a custom SCC for a third-party image,
this relay is not deployable there. That is a real answer, and it is better than
finding out through a `CrashLoopBackOff` at the end of a rollout.

## What to do about it

Apply the SCC in this directory and bind it to a dedicated ServiceAccount for
the relay only.

```sh
# 1. Create the SCC (cluster-admin required, once per cluster).
oc apply -f scc.yaml

# 2. Create a ServiceAccount for the relay in your namespace.
oc create serviceaccount mailkube-smtp-relay -n <namespace>

# 3. Bind the SCC to that ServiceAccount, and nothing else.
oc adm policy add-scc-to-user mailkube-smtp-relay \
    -z mailkube-smtp-relay -n <namespace>
```

Then deploy the manifests from [`../kubernetes/`](../kubernetes/) with one
addition to the pod spec:

```yaml
spec:
  serviceAccountName: mailkube-smtp-relay
```

Nothing else changes. The `securityContext` in those manifests is already the
minimum this SCC permits: root UID, `readOnlyRootFilesystem: true`,
`allowPrivilegeEscalation: false`, all capabilities dropped, and exactly six
added back.

### Verify the binding took effect

```sh
oc get pod <relay-pod> -o jsonpath='{.metadata.annotations.openshift\.io/scc}'
```

That must print `mailkube-smtp-relay`. If it prints `restricted-v2`, the pod is
not using your ServiceAccount, and it will not start.

## Use the narrowest grant available

- **Bind with `-z <serviceaccount>`**, never
  `add-scc-to-group system:authenticated` and never
  `add-scc-to-user ... system:serviceaccount:*`. The grant should cover one
  ServiceAccount in one namespace.
- **Do not reach for `anyuid` or `privileged`.** Both are much wider than what
  this image needs: `anyuid` grants an arbitrary UID but leaves the default
  capability set in place, and `privileged` grants effectively everything. The
  SCC here grants an arbitrary UID plus six named capabilities and denies host
  network, host PID, host IPC, host paths, host ports, and privilege escalation.
- **Sidecar mode changes the blast radius.** In sidecar mode the SCC is bound to
  your *application's* ServiceAccount, because the relay runs inside the
  application's pod, so the application container gains the same permissions.
  If that is not acceptable, use the cluster-service (StatefulSet) topology,
  where the elevated ServiceAccount is used by the relay pods alone.

## Two things that are not OpenShift specific but bite here first

- **The NetworkPolicy in `../kubernetes/30-networkpolicy.yaml` still matters.**
  The relay's SMTP listener is unauthenticated by design. OpenShift's default
  network policy in a new project usually allows all intra-project traffic, so
  applying that file is what actually restricts who can submit mail.
- **Persistent volumes must support UNIX domain sockets.** Postfix creates
  sockets under `private/` and `public/` in its queue directory. Prefer a
  block-backed RWO StorageClass. Several NFS-backed storage classes cannot
  create a socket in the volume, and Postfix fails at startup.
