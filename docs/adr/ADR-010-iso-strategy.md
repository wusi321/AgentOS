# ADR-010: Cubic for the ISO prototype, a repeatable chain for production

**Status:** accepted
**Date:** 2026-10-13

## Context

The delivery target includes a customised ISO with AgentOS preinstalled. The first
release ships `.deb` packages only. The ISO phase has to choose how to build one,
and has to be honest that a graphical remastering tool is not a production build
chain.

## Options considered

1. **`live-build`.** The familiar Debian way to build a live image.
2. **Cubic** (Custom Ubuntu ISO Creator). A graphical chroot-based remastering tool
   for an existing Ubuntu ISO.
3. **`livecd-rootfs` / `ubuntu-cdimage`.** Canonical's own Desktop image build
   chain.
4. **`autoinstall` plus a package repository.** Install stock Ubuntu and let the
   installer add AgentOS from a signed repository.

## Decision

For the prototype: **Cubic**, on the official Ubuntu 22.04 Desktop ISO, with the
built `.deb` files and the default dconf settings.

For production: a **repeatable chain** built on Canonical's own processes, chosen
after the prototype establishes what actually has to change in the image. The
candidates to evaluate are `livecd-rootfs`/`ubuntu-cdimage` and an `autoinstall`
path with a signed local repository.

## Reasoning

**Why Cubic for the prototype.** It is fast and its output is genuinely installable,
which is what an early prototype needs. What it is not is auditable: its
configuration lives in a GUI session, not in a reviewable file, so its result cannot
be rebuilt from source control. The design outline says the same thing, and this ADR
records the consequence: a Cubic image is a verification artifact, never a release
artifact.

**Why not `live-build` by default.** It builds Debian-style live images, and Ubuntu
Desktop's installer, its curated package set, and its `casper` configuration are
Canonical's own chain. Assuming `live-build` can replace that is the specific
mistake the design outline warns against. It stays a research candidate for a
*live* environment, not for a Desktop installer image.

**Why evaluate `autoinstall` as a production option.** It may turn out that almost
nothing in the image needs to change: if AgentOS is a metapackage plus a dconf
default plus a first-boot wizard, then a stock ISO with an `autoinstall` answer file
and a signed local repository produces a machine that is bit-for-bit stock Ubuntu
apart from the packages. That is a materially better story for security updates
than a remastered image, because the base stays Canonical's.

**The decision that actually matters is what goes in the image.** The prototype is
how we find out, so the prototype is scoped to answer that question rather than to
look finished.

## What the image must contain

| Item | Why |
|---|---|
| the Ubuntu base version and its SHA-256 | the build is not reproducible without it |
| the preinstalled `.deb` list with versions and checksums | the release manifest |
| the GNOME extension deployment and its default enablement | the extension cannot be enabled by a postinst |
| default dconf/GSettings settings | how the extension starts enabled for a new user |
| the first-boot wizard and its privacy and credential explanation | the design requires an explicit, optional model setup |
| the package repository and its offline dependency list | an offline install has to resolve everything |
| the source commit, build parameters, and checksums | traceability |

## What the image must not contain

* a developer's credential, SSH key, or session;
* a test database or a machine identity copied to every installation;
* a pre-authorised system-write policy: the default stays read-only;
* a forced model provider — model configuration is an optional first-run step.

A first boot has to generate its own instance data. Copying a build machine's state
into every installation would leak one machine's identifiers into many.

## Security impact

**Positive.** Keeping the base stock as far as possible means Canonical's security
updates apply normally. A remastered image diverges from that, and the divergence has
to be maintained by hand for every Ubuntu point release.

**Positive, specific.** The design requires that a missing or unreachable AgentOS
service must not prevent login, the desktop, network settings, or a terminal from
working. That has to be tested on the image, not assumed: the test is to boot with
`agentos-core` disabled and confirm the system is completely usable.

**Negative and accepted.** An ISO is a large, hard-to-audit artifact. Until the
production chain exists, the supported installation path remains `agentos-meta` on
stock Ubuntu, and this ADR says so rather than treating the prototype as a release.

## Maintenance cost

The prototype's cost is one GUI session per rebuild, which is why it is not the
production answer. The production chain's cost is real but bounded: a versioned
input file, a build runner, and an install-and-boot test in a VM. The design's
acceptance criteria for the phase require the automated test to cover installation,
first login, extension startup, core status, and a runtime smoke test.

## Review trigger

This ADR is superseded by the ADR that chooses the production ISO chain. That choice
should be made after the prototype answers three questions:

1. Can the extension be enabled for a new user through dconf defaults alone, or
   does the first-boot wizard have to do it?
2. Does the wizard need anything from the image that a stock Ubuntu plus packages
   cannot provide?
3. Is the image's divergence from stock Ubuntu small enough that `autoinstall` plus
   a repository is sufficient?

If the answer to (3) is yes, the production chain should be the simplest one that
exists, and this project should not maintain an image build at all.
