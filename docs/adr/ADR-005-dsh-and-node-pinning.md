# ADR-005: Pin the DSH revision, and treat Node.js as a deployment choice

**Status:** accepted
**Date:** 2026-10-13

## Context

DeepSeek Harness is pre-stable upstream: its interfaces may change incompatibly,
and its Node.js engine requirement changes with it. The target platform is Ubuntu
22.04 and 24.04, whose distribution Node.js versions are far older than the
harness needs.

## The facts, measured

| Fact | Value |
|---|---|
| Locked version | `0.2.1-alpha.2` |
| Locked tag | `dsh-v0.2.1-alpha.2` |
| Locked commit | `d743267388641bc76f17c45ce8b4c231aed1d32c` |
| Engine requirement | `node: ^22.19.0 \|\| >=24.0.0` |
| Ubuntu 22.04 `nodejs` | 18.x or older, depending on the image |
| Ubuntu 24.04 `nodejs` | 18.19.1 |
| Verified working Node.js | 22.22.2 |

## Options considered

1. **Depend on `nodejs` and hope.** Write `Depends: nodejs` and let the harness
   fail on an old interpreter.
2. **Bundle a Node.js runtime** inside `agentos-runtime-dsh`.
3. **Maintain an APT source** for a supported Node.js, and depend on it.
4. **Pin the harness, treat Node.js as the deployment's choice, and refuse to run
   on an unsupported interpreter.**

## Decision

Option 4.

* The harness revision is pinned by version **and** commit, in code, in the
  packaging metadata, and in the documentation.
* `agentos-runtime-dsh` declares `Recommends: nodejs (>= 22.19.0)` and **not** a
  hard dependency.
* The adapter checks the interpreter at startup and refuses to run when the
  requirement is unmet, reporting the exact requirement and the version it found.
* `scripts/probe-dsh-sdk.sh` reproduces the verification on demand.

## Reasoning

* **Option 1 is a silent failure.** An unsupported interpreter produces confusing
  errors from deep inside the harness. Refusing at the adapter boundary, with the
  requirement and the observed version in the message, is the difference between a
  five-minute fix and an afternoon.
* **Option 2 was rejected on volume and on security-update cost.** Bundling a Node
  runtime adds tens of megabytes to a package, freezes the interpreter at whatever
  security patch level was current at build time, and makes this project responsible
  for shipping interpreter CVEs. The design outline requires a security-update
  policy for a bundled runtime, and there is no team here to run it.
* **Option 3 was rejected for offline installability.** Depending on an external
  APT source makes installation depend on network and third-party repository state,
  which an ISO cannot guarantee.
* **Option 4 keeps each party's responsibility where it belongs.** The deployment
  chooses and maintains its interpreter, and inherits that distribution's security
  policy. AgentOS states the requirement, checks it, and refuses to guess.
* **A hard `Depends` was specifically rejected** because no Node.js in either target
  release satisfies the requirement. A hard dependency would make the package
  **uninstallable** on the target platform — the worst possible outcome, and one
  that a naive "declare the dependency" instinct produces immediately.

## Security impact

* A deployment can install any interpreter it likes, including a vulnerable one.
  The mitigation is that the adapter checks the **version**, not a build hash, and
  that `agentosctl doctor` reports the requirement and the observed version
  prominently.
* No interpreter is redistributed, so no interpreter CVE lands in this project's
  release notes.
* The pinned commit makes the verified behaviour reproducible: a reader can check
  out `d743267` and re-run the probe.

## Maintenance cost

Low, but it cannot be skipped. Upgrading the harness is a deliberate sequence:

1. install the new revision and record its tag and commit;
2. update `DSH_LOCKED_VERSION` and `DSH_LOCKED_COMMIT` in `constants.py`, in
   `packaging/dsh/dsh-locked.json`, and in `docs/dsh-integration.md`;
3. re-run `scripts/probe-dsh-sdk.sh`;
4. re-run the adapter contract tests (`pytest tests/contract -q`);
5. record what changed.

The cost of *not* doing that is a runtime that behaves differently from its
documentation, which is the failure this ADR exists to prevent.

## Review trigger

Revisit when a Node.js satisfying the requirement is available in the Ubuntu
releases this project targets, at which point a versioned `Depends` becomes correct
and the Recommends should be tightened. Also revisit if the probe cannot be run in
the project's own CI, because then the pin is a claim rather than a verification.
