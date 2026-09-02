# Connecting an agent

The agent runs next to your code and is the only thing that writes to it. This is how to get one
connected, on Windows, macOS or Linux.

**You do not need Go, and you do not build anything.** `make build-agent` exists for people working
on the agent itself; it is not the way to install one.

**The screen is the source of truth.** Open **Onboarding** (or **Pairing**) in the ForgeOps UI, pick
your project, and it prints all three commands below already filled in for your platform and shell —
including the `--backend` value, which it computes from the deployment you are looking at. Copy
buttons are next to each one. Everything here is the same thing written down.

## 1. Download the agent

Take the archive for your platform from the release the UI links to. Both architectures are offered
because a browser cannot tell reliably which one you are on:

| Platform | Intel/AMD                                      | Apple Silicon / ARM                            |
| :------- | :--------------------------------------------- | :--------------------------------------------- |
| Windows  | `forgeops-agent_<version>_windows_amd64.zip`   | `forgeops-agent_<version>_windows_arm64.zip`   |
| macOS    | `forgeops-agent_<version>_darwin_amd64.tar.gz` | `forgeops-agent_<version>_darwin_arm64.tar.gz` |
| Linux    | `forgeops-agent_<version>_linux_amd64.tar.gz`  | `forgeops-agent_<version>_linux_arm64.tar.gz`  |

Every archive is signed keyless with Cosign and published with a CycloneDX SBOM. `make
verify-release` in the [README](../README.md#verifying-a-release) checks both without needing a
shared secret.

If the UI says this deployment publishes no download, whoever operates it has not pinned a release;
ask them for the binary rather than building your own.

## 2. Put it on your PATH

One step, so the bare `forgeops-agent` command works from any directory afterwards.

**Windows (PowerShell)** — no elevation needed. `%LOCALAPPDATA%\Programs` is already on PATH for
your user on a default install:

```powershell
New-Item -ItemType Directory -Force $env:LOCALAPPDATA\Programs\ForgeOps | Out-Null
Move-Item -Force .\forgeops-agent.exe $env:LOCALAPPDATA\Programs\ForgeOps\
```

**macOS and Linux** — `install` sets the mode in the same step, so there is no separate `chmod` to
forget:

```sh
sudo install -m 0755 ./forgeops-agent /usr/local/bin/forgeops-agent
```

Check it:

```sh
forgeops-agent version
```

If that reports "not recognized" on Windows, the move did not land where PATH looks — open a new
terminal first, since PATH is read at shell start.

## 3. Connect

Mint a pairing code in the UI, then run the one command it prints:

```sh
forgeops-agent connect --code <code> --backend <url>
```

It does three things and says so as it goes:

```
[1/3] pair   device 01JB…, credentials in keychain
[2/3] scan   412 file(s), 1980 chunk(s), 37 dependency edge(s), 0 redaction(s)
[3/3] run    holding the session open for project …; press Ctrl+C to stop
```

It stays running. That is deliberate — an approved change set is applied by a live agent, so closing
the terminal stops changes being applied.

The individual verbs all still work, and `connect` gains no authority they do not have: `pair`,
`scan`, `run` and `watch` do exactly what they did before.

### About the code's five minutes

A pairing code is single-use and expires in five minutes; the screen shows a live countdown and
offers a new one in place when it runs out. Do steps 1 and 2 **before** you mint a code and five
minutes is more than enough — the reason the window used to be tight is that installing the agent
meant compiling it.

## The two ports, and why you only ever type one

An agent talks to ForgeOps over **two** connections, and they have different authentication rules:

|                  | where                               | authenticated by                              |
| ---------------- | ----------------------------------- | --------------------------------------------- |
| pairing          | the ordinary API port, e.g. `18000` | nothing — it is the one unauthenticated route |
| session and scan | the agent listener, e.g. `18443`    | a client certificate **and** a device token   |

Pairing cannot require a certificate, because pairing is where the agent is _issued_ one. The session
must require it, because that is what authorises a command to write to your disk. Requiring a client
certificate is a property of a listener rather than of a route, so they are two listeners.

**You only type the first.** The pairing response carries the CA bundle _and_ the address of the
second, so the agent learns where to hold its session from the backend that just certified it. If
your `--backend` names the ordinary port, everything below works with nothing else configured.

`docker compose ps backend-agent` should show that listener published:

```
forgeops-backend-agent-1   Up (healthy)   127.0.0.1:18443->8443/tcp
```

If it is missing, `make up` started a stack that cannot deliver an approved change set — the agent
will pair and then sit there while change sets stay `applying`. `AGENT_TLS_HOST_PORT` moves the host
side of that mapping.

## Checking that a change set can actually reach you

`forgeops-agent doctor` answers this before you spend a code, and it answers it by completing a real
handshake rather than by reading configuration:

```
✓ Credential store: keychain, and a device credential fits
✓ Pairing: device eb85ecea-… (credentials: keychain)
  client certificate expires 2026-09-03T02:30:49Z
  session endpoint: wss://localhost:18443/api/v1/ws/agent — stated by the backend at pairing
✓ Session trust: verified against the CA bundle stored at pairing
  issuer: ForgeOps Internal Device CA
  CA expires 2036-08-18T03:01:06Z
```

Three things are worth reading there. The endpoint is **stated by the backend**, not guessed. The
trust source is the CA you were issued at pairing — not your machine's trust store, which has never
heard of this CA and does not need to. And the tick means a TLS 1.3 handshake completed against that
port with your device certificate presented.

If it cannot, it says so and does not proceed:

```
✗ Session trust: cannot verify the backend's listener
  verifying against: the CA bundle stored at pairing
  dial tcp [::1]:18443: connectex: No connection could be made…
```

There is no flag to skip that verification, on any platform. The certificate _is_ the authentication
for this connection; an unverified channel to it is not a degraded version of the feature.

## What an applied change set does to your files

When you approve a change set, the running agent:

1. checks the pre-image — it hashes each file it is about to overwrite and **aborts the whole set**
   if any hash disagrees with what the change set was built against, so a file you edited meanwhile
   is never silently clobbered;
2. takes a backup of every file it overwrites, named `<file>.backup.<UTC timestamp>Z`;
3. writes the new contents;
4. reports the result, which moves the change set `applying → applied`.

If a write fails partway, it restores what it had already changed from those backups and the change
set becomes **`rolled_back`** — a different state from `reverted`, which is what you get when you
deliberately reverse an applied change. `rolled_back` means your working tree is as it was;
`reverted` means somebody undid something that had succeeded.

### On Windows

Paths in a change set travel in POSIX form and are materialised natively, so `deploy/Dockerfile`
becomes `deploy\Dockerfile` and any missing directory is created.

One thing to know if you are preparing fixture files by hand: **PowerShell's `Set-Content -Encoding
utf8` writes a byte-order mark**, which changes the file's hash. Step 1 above will then correctly
refuse the apply. Use `[System.IO.File]::WriteAllText($path, $text, (New-Object
System.Text.UTF8Encoding($false)))` if you need exact bytes.

File permissions are not carried. A change set records no mode, and the agent's owner-only checks are
skipped on Windows deliberately: NTFS ACLs are not POSIX `0600`, and reporting a synthetic `0666`
as if it meant something would be worse than saying nothing.

### On Linux

Written files take the default `0644` and directories `0755`. If the agent runs as a different user
from the one that owns the checkout, an apply fails while creating its temporary file rather than
while writing the target — `mutate` writes to a temp file in the target directory and renames, so it
needs to _create_ in that directory:

```
create temp for Dockerfile: open /workspace/.forgeops-206260492: permission denied
```

The agent reports that as a rollback, so nothing is half-written. Fix it by giving the agent's user
write access to the directory (`chmod a+rwX`), not by running the agent as root.

## Where `--backend` comes from

The agent will not guess a backend URL. A device token is a bearer credential, so an agent that
invented a host could hand one to whatever answered. It looks in three places, first hit wins:

1. `--backend` on the command line
2. the `AGENT_BACKEND_WSS_URL` environment variable
3. `BACKEND_PORT` in a `.env` file in the current directory or up to four directories above it

The third is for developers running the agent from the repository that started the stack: it needs no
flag at all. A value found that way must be **loopback** — a file cannot point your agent at somebody
else's machine. An explicit flag may name any host, because you typed it.

Whichever answered is printed, so you can always see which host is being dialled:

```
Using backend ws://localhost:18000/api/v1/ws/agent (from BACKEND_PORT in .env).
```

If none of the three has a value, the error names all three and points at the UI, which knows.

## When something is wrong

```sh
forgeops-agent doctor
```

It reports Docker, Kubernetes, OpenTofu, the pairing state, and — the one worth reading before you
spend a code — **the credential store and whether a device credential will fit in it**:

```
OK  Credential store: keychain, and a device credential fits
```

That check performs a real trial write, so it is a fact about your machine rather than an assumption.
If it says the store cannot hold a credential, set `AGENT_CREDENTIAL_STORE=file` and the agent keeps
the credential in a `0600` file under its state directory instead.

### Common answers

**"pair: --code is required"** — mint one in the UI. Codes are shown once and stored only as an
HMAC, so there is no way to show one again.

**"the pairing code is not valid: issue a new code and try again"** — it expired, or it was already
used. Mint another; the screen has a button.

**"stored credentials are incomplete"** — a device token exists with no certificate beside it, which
happens if the state directory was cleared while the OS keychain entry survived. Run `forgeops-agent
pair --wipe` and pair again.

**Pairing failed and you are not sure whether a device was created** — the agent checks that it can
store a credential _before_ it spends the code, and if a write fails anyway it surrenders the device
so the backend is not left holding one. If both fail, the error says the device may still be active
and names it, so it can be revoked from the UI.

## Keeping the index fresh

`connect` indexes once at startup. To re-index as you edit:

```sh
forgeops-agent watch --project <id>
```

Or once, on demand:

```sh
forgeops-agent scan --project <id>
```

## Where the credential lives

Split by what is actually secret:

- the device token, the envelope key and the client **private** key go in the OS credential store —
  Windows Credential Manager, macOS Keychain, or libsecret;
- the client certificate, the CA bundle and the pinned policy bundle go in a `0600` file beside it,
  because a certificate is published to any peer that completes a handshake and the policy bundle is
  signed policy the backend serves to every device pinned to that digest.

The split is not cosmetic. Windows Credential Manager refuses anything over 2560 bytes, and the whole
set is 28014 — so before this, pairing on Windows failed _after_ the exchange had spent the code. The
secret half is 485 bytes.

`forgeops-agent pair --wipe` removes both halves and needs no backend, so it works when you cannot
reach one.
