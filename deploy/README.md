# Lightsail runbook

Run one named step at a time and stop when it fails. The commands below create
one Ubuntu 24.04 x86-64 Lightsail instance, install PostgreSQL 16 and Caddy, and
serve the React frontend and Python backend. DNS is manual. Application updates
keep the old release serving until the candidate is ready.

`manage.py` supplies the configured names, paths and credentials to each step.
It does not choose which cloud resources to create next or reconcile a partially
completed installation. `bringup`, `provision` and `destroy` are retired; they
print guidance without changing anything.

## Inputs and prerequisites

Copy `deploy/settings.example.py` to **`deploy/private/settings.py`** and edit it.
Run every command in this document from the repository root (`E:\bz` on this
workstation), using PowerShell or a POSIX shell.

```python
NAME = "draft-preprod"
REGION = "us-west-2"
HOSTNAME = "slaprat.click"
SSH_CIDRS = ["YOUR_PUBLIC_IPV4/32"]
GOOGLE_OIDC_JSON = r'''{
  "web": {
    "client_id": "...apps.googleusercontent.com",
    "client_secret": "...",
    "redirect_uris": ["https://slaprat.click/g"]
  }
}'''
```

Use the complete downloaded Google **web application** JSON. The callback is
selected from the registered HTTPS redirect URI for `HOSTNAME`; set
`OIDC_CALLBACK_PATH` if several match. Register that exact URI in Google Cloud.
For a consent screen in testing mode, add the intended accounts as test users.

The example also documents the optional AWS profile and Availability Zone,
instance bundle, catalog CSV and image directory, and pinned uv/Node versions.
Relative input paths resolve against the settings module's directory. The host
installer supports Ubuntu 24.04 x86-64. Without `AVAILABILITY_ZONE`, instance
creation uses `REGION` followed by `a` (for example `us-west-2a`).

You need:

- uv, Git and OpenSSH (`ssh` and `scp`) on the workstation.
- AWS credentials with the Lightsail operations used by `cloud.py` and
  `sts:GetCallerIdentity`. Credentials stay on the workstation.
- A registered domain, access to its DNS records and the Google client JSON.
- The catalog CSV and its referenced PNGs. Images are imported into PostgreSQL.

No local Node or PostgreSQL installation is needed. The scripts do not edit IAM
permissions or DNS records.

The ignored `deploy/private/` directory contains the settings module, SSH private
key, verified `known_hosts`, `state.json` resource receipt and generated database
and session secrets. Keep these files for the lifetime of the host. The default
state directory is `deploy/private/NAME`; use a separate directory for a different
name, region, hostname or AWS account. Existing version 1 receipts and SSH keys
remain usable.

`create-key` generates the local RSA key if absent and imports its public key to
Lightsail. Only the SSH private key receives restrictive OpenSSH permissions;
the settings and state directory retain normal inherited filesystem permissions.
`setup` generates application secrets once and reuses them on later runs.
`trust-host` obtains SSH host keys through authenticated AWS and writes
`known_hosts`; SSH host checking stays enabled.

## Check and inspect the runbook

```text
uv sync --project deploy --locked
uv run --project deploy python deploy/manage.py check
uv run --project deploy python deploy/manage.py steps
```

`check` validates local inputs and tools without calling AWS. `steps` prints the
sequence without executing it. If the default uv cache is unavailable, add
`--cache-dir deploy/.uv-cache` to uv commands. To use another input module, put
`--config PATH` before the action.

## Create a new machine

Run these only for a new deployment. Each command performs its named operation;
creation commands stop if their resource already exists.

```text
uv run --project deploy python deploy/manage.py create-key
uv run --project deploy python deploy/manage.py create-instance
uv run --project deploy python deploy/manage.py open-ports
uv run --project deploy python deploy/manage.py allocate-ip
uv run --project deploy python deploy/manage.py attach-ip
uv run --project deploy python deploy/manage.py trust-host
uv run --project deploy python deploy/manage.py cloud-status
```

`open-ports` applies the configured SSH CIDRs and public HTTP/HTTPS access.
`cloud-status` reads AWS and shows the recorded resources and IP. If a command
fails, inspect its output and `cloud-status`, then run the exact remaining step.
Do not rerun all creation commands to resume. A conflicting resource or an AWS
operation with an uncertain outcome needs inspection in AWS before continuing.
Fix the failed step and rerun that step only. `trust-host` also stops if AWS has
not reported the host keys yet; retry it once the instance is ready. Keep the
receipt and key; deleting them does not resolve a resource conflict.

## Install the host and first release

**For the interrupted `bringup` that already created the machine and trusted its
host key, start here with `setup`. Do not recreate the cloud resources.** `setup`
uses the existing `state.json`, SSH key and `known_hosts` without contacting AWS.
Run `trust-host` only if `known_hosts` is absent or a replacement host needs its
new key verified through AWS.

```text
uv run --project deploy python deploy/manage.py setup
uv run --project deploy python deploy/manage.py prepare
uv run --project deploy python deploy/manage.py init-db
uv run --project deploy python deploy/manage.py import-catalog
uv run --project deploy python deploy/manage.py deploy
```

These are five separate steps:

1. `setup` checks the SSH connection and waits for Ubuntu first boot, copies the
   readable scripts in `deploy/host/`, and runs `bootstrap.py` to install host
   services and runtimes.
2. `prepare` pushes a Git revision and installs/builds its locked dependencies.
3. `init-db` runs the application's initializer on the fresh database.
4. `import-catalog` uploads the catalog and referenced images, then runs the
   application's importer.
5. `deploy` starts the candidate, checks readiness, switches Caddy and retires the
   previous release. It prepares the selected revision if needed.

Commit the intended app changes before preparing a release. The default revision
is `HEAD`; use `--revision FULL_COMMIT_ID` on `prepare`, `init-db`, `import-catalog`
and `deploy` to pin the same source revision across steps. These commands refuse
dirty application/deployment directories by default. Add `--allow-dirty` to each
if intentionally deploying the selected **committed** revision while local edits
exist; uncommitted app files are excluded. `setup` uses the local `deploy/host/`
scripts, so changes to the installer do not require an application commit.

`setup`, release and database commands, `status`, `ssh` and `logs` use the saved
receipt and verified host keys; they do not call AWS. Host-key verification is
the separate `trust-host` step.

Commands print their nonsecret command lines and stream installation/build output.
Short values needed by the scripts, such as the upload-directory path, are
captured. Long workstation waits print elapsed progress every 15 seconds and
have finite time limits. `status: done` from `cloud-init` means only that Ubuntu's
first boot has finished; the named host installation step follows.

Press Ctrl-C once to cancel the local command. Remote work may still be running:
inspect it using `ssh` before rerunning that step. Failed or interrupted uploads
remain at the printed remote path for inspection; cancellation does not start a
second SSH cleanup command. Saved keys and deployment files are retained.

## Point DNS and check the site

Read the IP from `cloud-status`. Save the current DNS record, then set the
hostname's A record to that IP yourself. Remove or migrate any AAAA/CNAME records
that would send clients elsewhere. Allow old DNS caches to expire and Caddy to
obtain its HTTPS certificate, then run:

```text
uv run --project deploy python deploy/manage.py smoke
```

`smoke` checks public HTTPS `/` and `/ready`. Also sign in with Google, inspect
catalog images, and create a league and draft in two browser sessions to check
writes and live updates. DNS records are never saved or restored by the scripts;
remove `HOSTED_ZONE_ID` if upgrading an older settings module.

## Update, inspect or roll back

```text
uv run --project deploy python deploy/manage.py prepare --revision FULL_COMMIT_ID
uv run --project deploy python deploy/manage.py deploy --revision FULL_COMMIT_ID
uv run --project deploy python deploy/manage.py status
uv run --project deploy python deploy/manage.py logs app --revision FULL_COMMIT_ID
uv run --project deploy python deploy/manage.py logs caddy
uv run --project deploy python deploy/manage.py logs postgres
uv run --project deploy python deploy/manage.py ssh
uv run --project deploy python deploy/manage.py rollback --revision PREVIOUS_FULL_COMMIT_ID
```

Each release has its own virtual environment, service and Unix socket. Build and
startup happen while the old release serves traffic. Once the candidate is ready,
Caddy switches its frontend root and backend socket together. The new route is
saved for reboot before previous services stop. Old hashed frontend assets remain
available, and the session secret stays the same. Event streams can reconnect
during handoff. Adjacent releases must have compatible API and database contracts.

A failure before handoff leaves the old route and service in place. `status`
shows the live and saved route and whether they agree. If interrupted after Caddy
accepts the new route, rerun `deploy --revision ...` to finish persistence and
cleanup. A failed candidate is retained for log inspection and may keep restarting;
if abandoning it, stop it explicitly in a server shell:

```sh
sudo systemctl disable --now NAME@FULL_COMMIT_ID.service
```

Ordinary releases never initialize, reset or migrate the database. `init-db`
checks its completion marker and preserves initialized data. If initialization
stopped after creating the database but before writing that marker, it refuses
the unmarked database; inspect the failure and rebuild the disposable host rather
than expecting an automatic reset. Catalog import requires completed initialization
and upserts without deleting omitted rows/assets. Code rollback does not undo
database changes.

## Delete the deployment

This is one machine with its database on local storage. Deleting the instance
loses that database. Restore or remove the DNS record yourself first and allow
cached records to expire if clients must finish using the old host.

Run each deletion explicitly, using the exact configured `NAME` as confirmation:

```text
uv run --project deploy python deploy/manage.py delete-instance --confirm draft-preprod
uv run --project deploy python deploy/manage.py release-ip --confirm draft-preprod
uv run --project deploy python deploy/manage.py delete-key --confirm draft-preprod
uv run --project deploy python deploy/manage.py cloud-status
```

These steps check the recorded resource identity before deletion. `release-ip`
refuses an attached IP: delete the instance first. They do not discover or delete
resources from other deployments. Keep the receipt until all three steps finish.
The local settings, SSH key and application secrets are retained. To generate new
database/session secrets for a replacement host, remove only `secrets.json` from
this deployment's state directory after all cloud resources have been deleted.

## Verification and implementation

```text
uv run --project deploy python -m pytest deploy/tests -q
uv run --project deploy ruff check deploy
```

Local tests use fake AWS clients and host commands. They do not provision or
contact a server. Live acceptance still needs a fresh installation, Google login,
two-browser draft changes, a schema-compatible update and rollback, a failed
candidate, and service/reboot recovery. Check that data and sessions survive the
release changes, and that the saved release returns after reboot. Brief failures
during crashes/reboot are expected on this single host.

Read `cloud.py` for individual AWS actions, `remote.py` for SSH/copy commands, and
`host/bootstrap.py`, `host/database.py` and `host/release.py` for the scripts run
on the machine. The release handoff follows
[the reference spec](../caddy-uv-systemd-deployment.md). No automatic release or
shared-asset pruning, backups or database migration runner is included.
