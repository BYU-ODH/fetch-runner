# fetch-runner
Run scripts when `git fetch` finds new commits to specified git branches.

## Overview

Two users matter:

- **`[general].user`** — the user fetch-runner itself runs as (also
  `User=` in the systemd unit). fetch-runner refuses to start if the
  running uid doesn't match.
- **`[[jobs]].run_as`** — per-job; owns this job's repo and runs its git
  ops and deploy script. Defaults to `[general].user`. When set
  differently, fetch-runner dispatches everything via `sudo -n -u <run_as>`
  and a sudoers rule must allow it (generate one with
  `fetch-runner --print-sudoers <jobs.toml>`).

Convention assumed throughout the docs is that each repo lives at
`/srv/<run_as>/<repo_name>/`, owned `<run_as>:<run_as>` mode `0755`.
That gives every path under `/srv/<run_as>/` a single owner and lets
`[general].user` traverse with just search permission.

## Setup

### 1. Install fetch-runner

As `[general].user` (we use `fetch-runner` below):

```bash
uv tool install git+https://github.com/BYU-ODH/fetch-runner
```

Note the installed executable path (typically
`/home/fetch-runner/.local/bin/fetch-runner`).

### 2. Add a deploy script to each app

For a job with `run_as = "app1"` deploying the `api` repo:

```bash
sudo -u app1 cp /path/to/fetch-runner/examples/deploy.sh /srv/app1/api/deploy.sh
sudo -u app1 chmod +x /srv/app1/api/deploy.sh
```

Replace every `deploy-user` in the script with the job's `run_as` user
(`app1` here). The guard block at the top refuses to run as any other
user. Regenerate it for a different user with:

```bash
fetch-runner --print-guard app1
```

Commit the script to the app's repo so deploys are version-controlled.

### 3. Create the jobs config

```bash
cp /path/to/fetch-runner/examples/jobs.toml /home/fetch-runner/jobs.toml
```

Per `[[jobs]]`:
- `name` — label shown in logs
- `path` — absolute repo path, owned and writable by `run_as`
- `branch` — branch to watch
- `script` — absolute script path
- `run_as` — optional; defaults to `[general].user`
- `timeout_seconds` — optional script timeout

Validate without starting:

```bash
fetch-runner --check /home/fetch-runner/jobs.toml
```

### 4. Install the systemd service

```bash
sudo cp /path/to/fetch-runner/examples/fetch-runner.service \
    /etc/systemd/system/fetch-runner.service
```

In the `CUSTOMIZE` block, set:
- `User` / `Group` to `[general].user`
- `ExecStart` to the binary path from step 1 plus your config path
- `ReadWritePaths` to every directory any child process writes to —
  including the repos themselves (sudo'd git is still inside the unit's
  filesystem sandbox)

The example unit omits `NoNewPrivileges=` and `RestrictSUIDSGID=`
because they block sudo's setuid. The sudoers fragment (step 5) is what
bounds the privilege. If every job uses `run_as = [general].user`, you
can re-enable both.

### 5. Install the sudoers fragment (only if any job sets a different `run_as`)

```bash
fetch-runner --print-sudoers /home/fetch-runner/jobs.toml \
    | sudo tee /etc/sudoers.d/fetch-runner > /dev/null
sudo chmod 0440 /etc/sudoers.d/fetch-runner
sudo visudo -cf /etc/sudoers.d/fetch-runner  # syntax check
```

Re-run after any `jobs.toml` change. The git rule is intentionally not
arg-restricted: running git as `run_as` is no broader than what the
deploy-script rule already grants.

### 6. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fetch-runner
```

## Migrating from single-user mode

Existing configs without `run_as` keep working unchanged — sudo is
skipped entirely. To split, add `run_as` per job, update each script's
guard for the new user, regenerate the sudoers fragment, reload.

## Debugging

```bash
systemctl status fetch-runner
journalctl -u fetch-runner -f
journalctl -u fetch-runner -b
```

- `sudo: a password is required` → sudoers fragment is missing or stale;
  re-run step 5.
- `fetch-runner-guard: refusing to run as <user>` → the script's guard
  names a user that doesn't match the job's `run_as`; regenerate with
  `fetch-runner --print-guard <run_as>`.
