# Verifying AgentOS in a virtual machine

The automated verification runs in WSL on Ubuntu 24.04, which cannot exercise two
things: the GNOME Shell 42 extension, and the Python 3.10 interpreter that Ubuntu
22.04 ships. This page is the acceptance test to run on a real Ubuntu 22.04
Desktop machine or VM, and it is written so that every step either passes or gives
you the exact reason it did not.

## What you need

* an Ubuntu 22.04 LTS **Desktop** virtual machine (GNOME Shell 42), x86_64;
* the five `.deb` files and `SHA256SUMS` from the build;
* a snapshot of the VM taken **before** installing, so the test is repeatable.

The whole test needs no model credential and no network access. Only the optional
last section needs a credential.

## 0. Confirm the artifacts

```sh
sha256sum -c SHA256SUMS
ls -1 agentos-*.deb
```

Expected: five packages — `agentos-core`, `agentos-runtime-dsh`,
`agentos-desktop`, `agentos-gnome-extension`, `agentos-meta`.

## 1. Install

```sh
sudo apt update
# The desktop package needs GTK4, libadwaita, and the D-Bus Python bindings.
sudo apt install -y python3-psutil gnome-shell

sudo dpkg -i agentos-core_*.deb agentos-runtime-dsh_*.deb agentos-desktop_*.deb \
             agentos-gnome-extension_*.deb agentos-meta_*.deb
sudo apt install -f          # only if dpkg reported a missing dependency
```

The install must print a message telling you how to enable the extension. That
message is the expected behaviour: a package postinst cannot enable a per-user
Shell extension.

**Check.**

| Check | Expected |
|---|---|
| `dpkg -l 'agentos*'` | all five show `ii` |
| `getent passwd agentos` | `agentos:x:…:/nonexistent:/usr/sbin/nologin` |
| `stat -c '%a %U' /var/lib/agentos` | `700 agentos` |
| `ls /etc/agentos` | `agentos.toml`, `policies/`, `credentials.env.example` |
| `ls /etc/agentos/credentials.env` | **does not exist** — no package installs a credential |

## 2. The service starts and serves

```sh
sudo systemctl status agentos-core
sudo journalctl -u agentos-core -n 20
ls -l /run/agentos/core.sock
```

**Check.**

| Check | Expected |
|---|---|
| service state | `active (running)` |
| unit `User=` | `agentos`, never root |
| journal | `"message": "agentos-core listening"`, no traceback |
| socket | `srw-rw---- agentos agentos /run/agentos/core.sock` |

If `/proc/stat is not available on this system` appears in the journal when you
read metrics, the unit's sandbox is too strict. That exact failure was found and
fixed by removing `ProcSubset=pid`; confirm the installed unit does **not** set it:

```sh
grep -E 'ProcSubset|ProtectProc' /usr/lib/systemd/system/agentos-core.service
```

## 3. Access is authorized explicitly

```sh
agentosctl health                  # expect a refusal naming the agentos group
sudo adduser "$USER" agentos
# A new group membership needs a new session: log out and back in.
agentosctl health
agentosctl hello
```

**Check.**

| Check | Expected |
|---|---|
| before `adduser` | `ipc_access_denied`, with the message naming the `agentos` group |
| after a new login | `service ok=True` |
| `agentosctl hello` | prints `uid=` for **your** uid, from `SO_PEERCRED` |

## 4. Read-only system inspection

```sh
agentosctl system snapshot
agentosctl system processes --limit 10
agentosctl system disks
agentosctl system services --state failed
agentosctl system service ssh.service
agentosctl system logs --lines 20 --priority 4
```

**Check.** Each command prints real values from this machine, and the snapshot
reports 16 (or your VM's) cores, memory, and the real root filesystem. Every one of
these is a P0 read-only tool: none of them can change anything.

## 5. The policy posture is read-only

```sh
agentosctl policy inventory
agentosctl policy evaluate system.processes.list --params '{"limit": 5}'   # allow
agentosctl policy evaluate system.packages.install                          # deny
agentosctl policy evaluate system.imaginary.tool                            # deny
```

**Check.**

| Command | Expected |
|---|---|
| inventory | every tool with a level; P2 tools show `True` for approval; P3 tools show `DENIED` |
| the P0 read | `decision  allow` |
| a P3 tool | `decision  deny`, exit status 3 |
| an undeclared tool | `decision  deny` with "no capability manifest" |

## 6. A task runs end to end

```sh
agentosctl task run "Report whether this host is under memory or disk pressure." --watch
agentosctl task list --limit 5
agentosctl task history <task-id> | head -20
agentosctl audit summary
```

**Check.** The task reaches `SUCCEEDED`, the list shows it, the history contains
`task.created`, `runtime.session_created`, `runtime.message_delta`,
`runtime.message_completed`, `task.state_changed`, and the audit summary counts at
least one `task.created`.

With the shipped mock runtime, the answer is scripted. That is correct: it proves
the whole chain works with no credential and no network.

## 7. Closing the window does not stop a task

```sh
agentosctl task run "A long task that keeps running after the window closes."
# Note the task id, then:
agentosctl task list
# Open the desktop application, close its window, then:
agentosctl task list
```

**Check.** The task's state is unchanged by closing the window, and the service is
still `active`. Task lifetime belongs to the core, not to the UI.

## 8. The desktop application

```sh
agentos-desktop
# or launch "AgentOS" from the application grid
```

**Check.**

| Check | Expected |
|---|---|
| window | opens with six views: Chat, Tasks, System, Approvals, Activity, Settings |
| System view | shows this machine's real CPU, memory, and filesystems |
| Settings view | shows the locked DSH revision `0.2.1-alpha.2` and the policy inventory |
| closing the window | hides it; the application stays running and reopens instantly |
| `agentos-desktop --check` | reports that GTK4 and libadwaita bindings are available |

## 9. The GNOME Shell extension

```sh
gnome-extensions enable agentos@agentos.dev
gnome-extensions info agentos@agentos.dev
# Log out and back in.
```

**Check.**

| Check | Expected |
|---|---|
| `gnome-extensions info` | `State: ENABLED` |
| top bar | an AgentOS icon; a count appears when a task is active |
| clicking it | a menu with status, **Open AgentOS**, active tasks, approvals, recent activity |
| **Open AgentOS** | the desktop window appears |
| systemd user unit | `systemctl --user status agentos-session` is `active (running)` |
| `gdbus call --session --dest org.agentos.Session1 --object-path /org/agentos/Session1 --method org.agentos.Session1.GetStatus` | returns `('ok', '{...}')` with real task counts |

### 9a. The Shell stays stable when AgentOS is not

This is the requirement that matters most, and it is the one a screenshot cannot
show:

```sh
sudo systemctl stop agentos-core
# Look at the panel. It must show "AgentOS service unavailable", not crash.
sudo systemctl start agentos-core
# The panel must recover on its own within the poll interval.

systemctl --user stop agentos-session
# The panel must show the unavailable state again, and the Shell must be fine.
systemctl --user start agentos-session
```

**Check.** In every case the Shell keeps working: no error dialog, no `Alt+F2`
reload needed, windows still move, the overview still opens. Then:

```sh
sudo systemctl stop agentos-core
# Try to approve something from the panel: nothing should be approvable.
```

## 10. Notifications and throttling

```sh
# Cause a failure: point the runtime at a route that does not exist.
sudoedit /etc/agentos/agentos.local.toml     # set runtime_id = "dsh" with no credential
sudo systemctl restart agentos-core
agentosctl task run "this should fail"
```

**Check.** Exactly one notification appears for the failure. A second failing task
within five minutes does not produce a second notification for the same task.

Restore the configuration afterwards (`runtime_id = "mock"`) and restart.

## 11. Enabling a controlled operation, and the approval path

This is the part of the design that has to be seen to be believed: an operation is
refused, then enabled by an explicit policy, then requires an approval bound to one
target, then is decided.

```sh
# 11a. Under the shipped policy, the operation cannot even be requested.
TASK=$(agentosctl --json task run "prepare a controlled operation" --level-cap P2 | python3 -c 'import json,sys;print(json.load(sys.stdin)["task"]["task_id"])')
agentosctl approval request "$TASK" system.process.terminate --target $$
# expect: policy denied ... exceeds the maximum level this deployment permits (P1)

# 11b. Install the example site policy, which is what an administrator does.
sudo install -m 0644 /usr/share/doc/agentos-core/examples/../policies/50-site-policy.yaml.example \
                     /etc/agentos/policies/50-site-policy.yaml 2>/dev/null || \
sudo install -m 0644 /etc/agentos/policies/50-site-policy.yaml.example \
                     /etc/agentos/policies/50-site-policy.yaml
sudoedit /etc/agentos/policies/50-site-policy.yaml     # confirm the unit names
sudo systemctl reload agentos-core
agentosctl policy inventory | tail -3

# 11c. The same request now needs an approval.
agentosctl approval request "$TASK" system.process.terminate --target $$ --reason "vm verification"
agentosctl approval list
agentosctl approval show <approval-id>     # read the effect, the digest, and the nonce
```

**Check.**

| Check | Expected |
|---|---|
| before installing the policy | `policy denied` |
| `systemctl reload` | succeeds, and the journal reports the reload |
| after the policy | the same request returns an approval with a nonce |
| `approval show` | prints the operation, target, parameters, digest, and a plain statement of the effect |
| the effect text | names exactly the pid and says unsaved state is lost |

### 11d. The approval is bound, single-use, and revocable

```sh
agentosctl approval deny <approval-id> --note "verification"

# And a grant, which must produce a nonce:
agentosctl approval request "$TASK" system.process.terminate --target $$ --reason "second attempt"
agentosctl approval grant <approval-id-2>
agentosctl approval get <approval-id-2>       # state granted
agentosctl approval revoke <approval-id-2>    # state revoked
```

**Check.** After `revoke`, the approval cannot be used, and a second `revoke`
reports that it is not revocable. `audit query` shows `approval.requested`,
`approval.decided`, and the policy decisions.

Then remove the example policy so the machine is back to the shipped posture:

```sh
sudo rm /etc/agentos/policies/50-site-policy.yaml
sudo systemctl reload agentos-core
agentosctl policy evaluate system.process.terminate --params '{"pid": 1}'   # deny again
```

## 12. The keyless end-to-end demonstration

```sh
agentosctl demo
```

**Check.** It prints a health line, creates a task, shows it settling, shows the
policy allowing a read and denying an administrative tool, shows the approval
request being refused, and finishes with the audit summary. It exits 0.

## 13. The extension survives a Shell restart and a re-login

| Action | Expected |
|---|---|
| `Alt+F2`, `r`, Enter (Xorg only) | the panel entry returns; no extension error |
| Log out and back in | the panel entry returns and shows current task counts |
| Wayland session | everything above works; there is no `r` on Wayland, so log out instead |
| Disable then enable the extension | the panel entry disappears and returns cleanly |
| `journalctl /usr/bin/gnome-shell -n 100 \| grep -i agentos` | no unhandled exception from the extension |

## 14. Removal keeps the record, purge keeps it too

```sh
agentosctl task run "before removal" --watch
sudo ls -l /var/lib/agentos
sudo dpkg -r agentos-meta agentos-gnome-extension agentos-desktop agentos-runtime-dsh agentos-core
sudo ls -l /var/lib/agentos        # the database must still be there
sudo dpkg --purge agentos-core
sudo ls -l /var/lib/agentos        # still there, and purge said so
sudo sqlite3 /var/lib/agentos/agentos.sqlite3 'select count(*) from audit;'
```

**Check.** The database survives both, the messages say so, and the audit rows are
still readable. Only an explicit `sudo rm -rf /var/lib/agentos` removes them.

## 15. Optional: a real model

Only if you want to accept the two prerequisites:

1. a Node.js satisfying `^22.19.0 || >=24.0.0`. Ubuntu 22.04's `nodejs` does **not**;
2. the harness at the locked revision `0.2.1-alpha.2`
   (`d743267388641bc76f17c45ce8b4c231aed1d32c`).

```sh
node --version                      # must satisfy the requirement
agentosctl doctor                   # reports the requirement and what it found

sudo install -d -m 0755 /usr/lib/agentos/dsh
sudo npm install --prefix /usr/lib/agentos/dsh --no-audit --no-fund \
     @deepseek-ai/dsh@0.2.1-alpha.2

sudo install -m 0600 -o root -g root /dev/null /etc/agentos/credentials.env
sudoedit /etc/agentos/credentials.env        # add DEEPSEEK_API_KEY=...
sudoedit /etc/agentos/agentos.local.toml     # add the [runtime] block below
sudo systemctl restart agentos-core
```

```toml
# /etc/agentos/agentos.local.toml
[runtime]
runtime_id = "dsh"
provider = "deepseek-official"
model = "deepseek-flash"
node_binary = "/usr/bin/node"
cli_entry = "/usr/lib/agentos/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js"
```

**Check.**

| Check | Expected |
|---|---|
| `agentosctl doctor` | runtime `dsh`, `available=True`, credential present |
| `agentosctl runtime probe` | `cancel False`, `approval_events False`, `streaming True`, and a note explaining the cancellation limitation |
| `agentosctl task run "list the largest files under /var" --watch` | a real streamed answer with tool-call events |
| `agentosctl task run "…" ` with the credential removed | an explicit failure naming the missing credential, not a hang |
| `sudo chmod 644 /etc/agentos/credentials.env && sudo systemctl restart agentos-core` | the service **refuses** the file and says why |

Restore the credential's mode afterwards.

## What to report back

For each section: pass, or the exact command, its output, and the expected value it
contradicted. The two sections most worth running carefully are **9a** (the Shell
must survive AgentOS being down) and **11** (an operation must be refused, then
enabled by policy, then gated by a bound approval).
