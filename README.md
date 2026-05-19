# fetch-runner
Run scripts when `git fetch` finds new commits to specified git branches

## Setup

### 1. Install fetch-runner

As the deploy user (we use `my-app-user` in this document), install fetch-runner with `uv`:

```bash
uv tool install git+https://github.com/BYU-ODH/fetch-runner
```

Note the path of the installed executable (typically something like
`/home/my-app-user/.local/bin/fetch-runner`). You will need it in step 4.

### 2. Add a deploy script to your app

Copy `examples/deploy.sh` from this repository into your app's directory:

```bash
cp /path/to/fetch-runner/examples/deploy.sh /srv/myapp/deploy.sh
chmod +x /srv/myapp/deploy.sh
```

Open the script and replace every occurrence of `deploy-user` with the
username that runs your app's deployments (e.g. `my-app-user`). The guard block
at the top of the script prevents accidental execution as the wrong user or
as root.

Consider committing this script to your app's repository so the deploy
procedure is version-controlled alongside the code it deploys.

### 3. Create the jobs config

Copy the example config to the deploy user's home directory and edit it:

```bash
cp /path/to/fetch-runner/examples/jobs.toml /home/my-app-user/jobs.toml
```

Edit `/home/my-app-user/jobs.toml`:

- Set `user` under `[general]` to the deploy user (e.g. `"my-app-user"`).
  fetch-runner exits at startup if the running user does not match this value.
- Set `poll_interval_seconds` to how often fetch-runner should check for new
  commits (default: `60`).
- For each `[[jobs]]` entry, set:
  - `name` — a human-readable label shown in logs
  - `path` — absolute path to the local git repository to poll
  - `branch` — the branch to watch (e.g. `"main"` or `"production"`)
  - `script` — absolute path to the script to run when new commits are found
    (e.g. `/srv/myapp/deploy.sh`)
  - `timeout_seconds` — how long to let the script run before killing it
    (optional; omit to use the default)

### 4. Install and start the systemd service

Copy the example unit file to systemd's unit directory:

```bash
sudo cp /path/to/fetch-runner/examples/fetch-runner.service \
    /etc/systemd/system/fetch-runner.service
```

Edit `/etc/systemd/system/fetch-runner.service` and update the lines in the
`CUSTOMIZE` block:

- **`User` / `Group`** — set both to your deploy user (must match `user` in
  `jobs.toml`).
- **`ExecStart`** — replace `/usr/local/bin/fetch-runner` with the full path
  to the executable you noted in step 1, then replace
  `/etc/fetch-runner/jobs.toml` with the path to the config file from step 3.
  For example:
  ```
  ExecStart=/home/my-app-user/.local/bin/fetch-runner /home/my-app-user/jobs.toml
  ```
- **`ReadWritePaths`** — list every directory your deploy scripts need to
  write to (at minimum, the parent directories of your git repositories).
  Space-separate multiple paths, e.g.:
  ```
  ReadWritePaths=/srv/myapp /srv/anotherapp
  ```

Reload systemd and enable the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fetch-runner
```

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
