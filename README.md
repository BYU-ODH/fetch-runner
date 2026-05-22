# fetch-runner
Run scripts when `git fetch` finds new commits to specified git branches.

## Overview

fetch-runner uses **two distinct users**:

- The **polling user** (set under `[general].user` in the config and as
  `User=` in the systemd unit) is the user fetch-runner itself runs as.
  It owns nothing in the repos; it just runs the poll loop, validates
  scripts, and dispatches work via sudo.
- The **run-as user** (set per-job with `run_as` in the config) owns the
  local git repository for that job and is the user every git operation
  *and* the deploy script execute as. It defaults to the polling user
  when omitted, which preserves the original single-user mode.

When `run_as` matches the polling user, fetch-runner invokes git and the
script directly. When they differ, fetch-runner dispatches via
`sudo -n -u <run_as>` for both, and a narrow sudoers rule must allow
each combination. fetch-runner can generate that sudoers fragment for
you (see step 5 below).

### Filesystem ownership

For each job, with `run_as = "app1"`:

- The repo directory (e.g. `/srv/app1`) and everything under it is owned
  `app1:app1`. The polling user does not need write access anywhere in
  the repo.
- The polling user needs **search** permission (`x` bit) on the repo
  directory and its parents so it can `chdir` into the repo before
  exec'ing the deploy script. Standard mode `0755` is enough.
- The deploy script needs to be **readable** by the polling user (so it
  can run the post-checkout guard / permissions checks). Mode `0755` is
  the simplest choice; the guard refuses execution as any other user
  regardless.

## Setup

### 1. Install fetch-runner

Pick the user that will run fetch-runner itself (we use `fetch-runner` in
this document). As that user, install with `uv`:

```bash
uv tool install git+https://github.com/BYU-ODH/fetch-runner
```

Note the path of the installed executable (typically something like
`/home/fetch-runner/.local/bin/fetch-runner`). You will need it in step 4.

### 2. Add a deploy script to your app

Copy `examples/deploy.sh` from this repository into your app's directory:

```bash
cp /path/to/fetch-runner/examples/deploy.sh /srv/myapp/deploy.sh
chmod +x /srv/myapp/deploy.sh
```

Open the script and replace every occurrence of `deploy-user` with the
**run-as user** for this job — i.e. the account your app's deployment
should run as (e.g. `app1`). The guard block at the top of the script
prevents accidental execution as any other user, or as root.

To regenerate the canonical guard block for a given user:

```bash
fetch-runner --print-guard app1
```

Consider committing this script to your app's repository so the deploy
procedure is version-controlled alongside the code it deploys.

### 3. Create the jobs config

Copy the example config to the polling user's home directory and edit it:

```bash
cp /path/to/fetch-runner/examples/jobs.toml /home/fetch-runner/jobs.toml
```

Edit `/home/fetch-runner/jobs.toml`:

- Set `user` under `[general]` to the polling user (e.g. `"fetch-runner"`).
  fetch-runner exits at startup if the running process is not this user.
- Set `poll_interval_seconds` to how often to check for new commits
  (default: `60`).
- For each `[[jobs]]` entry, set:
  - `name` — a human-readable label shown in logs
  - `path` — absolute path to the local git repository to poll. The
    directory and its `.git` must be owned (and writable) by `run_as`,
    not by the polling user.
  - `branch` — the branch to watch (e.g. `"main"` or `"production"`)
  - `script` — absolute path to the script to run when new commits arrive
  - `run_as` — *optional;* the user that owns the repo, runs every git
    operation, and executes the deploy script. Defaults to
    `[general].user`. When set, sudoers must allow it (step 5).
  - `timeout_seconds` — optional; how long to let the script run before
    killing it.

Validate the config without starting the service:

```bash
fetch-runner --check /home/fetch-runner/jobs.toml
```

### 4. Install the systemd service

Copy the example unit file to systemd's unit directory:

```bash
sudo cp /path/to/fetch-runner/examples/fetch-runner.service \
    /etc/systemd/system/fetch-runner.service
```

Edit `/etc/systemd/system/fetch-runner.service` and update the lines in the
`CUSTOMIZE` block:

- **`User` / `Group`** — set both to the **polling user** (must match
  `[general].user` in `jobs.toml`).
- **`ExecStart`** — replace `/usr/local/bin/fetch-runner` with the full
  path noted in step 1, then replace the config path. For example:
  ```
  ExecStart=/home/fetch-runner/.local/bin/fetch-runner /home/fetch-runner/jobs.toml
  ```
- **`ReadWritePaths`** — list every directory the polling user needs to
  write to (at minimum, the parent directories of your git repositories).

**Security note**: the example unit deliberately omits `NoNewPrivileges=`
and `RestrictSUIDSGID=` because both block `sudo`'s setuid transition,
and fetch-runner relies on sudo when `run_as` differs from the polling
user. The narrow sudoers fragment (step 5) is what bounds what
fetch-runner can do with that privilege. If every job uses
`run_as = [general].user` (single-user mode), you can re-enable both
settings.

### 5. Install the sudoers fragment (only if any job uses a different `run_as`)

Generate the sudoers fragment from your config and install it:

```bash
fetch-runner --print-sudoers /home/fetch-runner/jobs.toml \
    | sudo tee /etc/sudoers.d/fetch-runner > /dev/null
sudo chmod 0440 /etc/sudoers.d/fetch-runner
sudo visudo -cf /etc/sudoers.d/fetch-runner   # syntax check
```

The output authorizes two operations per cross-user job: the git binary
(unrestricted args, but pinned to `(run_as)`) and the deploy script
(pinned absolutely). Re-run this command after any change to `jobs.toml`
so the sudoers file stays in sync.

The git rule is intentionally not arg-restricted. The escalation it
grants — "polling user can run git AS run_as" — is no broader than what
the deploy-script rule already permits, since the script also runs as
`run_as`. Tightening it with sudoers wildcards is brittle (any change to
fetch-runner's git argv breaks the rule with a confusing runtime error)
without meaningfully narrowing the threat.

If every job's `run_as` equals the polling user, the command prints a
no-op fragment and you can skip this step.

### 6. Enable and start the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fetch-runner
```

## Migrating from a single-user setup

Existing configs that do not set `run_as` keep working with no changes —
`run_as` defaults to `[general].user` and fetch-runner skips sudo
entirely in that case. To split a single-user setup, add `run_as` to one
job at a time, update that job's deploy script guard to name the new
run-as user, regenerate the sudoers fragment, and reload the service.

## Debugging

Check whether the service is running and see its recent log output:

```bash
systemctl status fetch-runner
```

Stream the full journal for the service (most useful when a deployment fails):

```bash
journalctl -u fetch-runner -f
```

To review all logs since the service last started:

```bash
journalctl -u fetch-runner -b
```

If a job fails with `sudo: a password is required`, the sudoers fragment
is missing or out of date — re-run step 5. If it fails with
`fetch-runner-guard: refusing to run as <user>`, the script's guard names
a different user than the job's `run_as`; regenerate the guard with
`fetch-runner --print-guard <run_as>`.
