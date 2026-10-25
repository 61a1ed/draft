# Lightsail runbook

Run one named step at a time and stop when it fails. The commands below create
one Ubuntu 24.04 x86-64 Lightsail instance, install PostgreSQL 16 and Caddy, and
serve the React frontend and Python backend. DNS is manual. Application updates
keep the old release serving until the candidate is ready.

Each signed-in browser tab now holds its own PostgreSQL notification connection.
The default per-worker limits are 30 streams (`SSE_MAX_CONNECTIONS`) plus 10
ordinary query connections (`DB_POOL_SIZE`). Budget both releases during a rollout,
all workers, and administrative/reserved connections before changing these limits.
The deployment does not raise PostgreSQL's connection limit. `/ready` checks the
query pool; stream failures reconnect independently. See the backend's
[stream operations and preproduction database upgrade procedure](../draft_backend/README.md#server-sent-events)
before deploying this schema change to an existing database.

`manage.py` supplies the configured names, paths and credentials to each step.
It does not choose which cloud resources to create next or reconcile a partially
completed installation. `state.json` records resource identities and connection
details; it does not prescribe the firewall or the next operation.

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
The backend checks callback syntax and route conflicts when starting.
For a consent screen in testing mode, add the intended accounts as test users.

The example also documents the optional AWS profile and Availability Zone,
instance bundle, catalog CSV and image directory, and pinned uv/Node versions.
Relative input paths resolve against the settings module's directory. The host
installer supports Ubuntu 24.04 x86-64. Without `AVAILABILITY_ZONE`, instance
creation uses `REGION` followed by `a` (for example `us-west-2a`).

You need:

- uv, Git and an updated OpenSSH (`ssh` and `scp`) on the workstation.
- AWS credentials with the Lightsail operations used by `cloud.py` and
  `sts:GetCallerIdentity`. Credentials stay on the workstation.
- A registered domain, access to its DNS records and the Google client JSON.
- The catalog CSV and its referenced PNGs. Images are imported into PostgreSQL.

No local Node or PostgreSQL installation is needed. The scripts do not edit IAM
permissions or DNS records.

On Windows, ensure the upgraded OpenSSH installation is ahead of the bundled
client in `PATH`. For this workstation, set it in the PowerShell session used
for deployment, then confirm the selected executables and version:

```powershell
$env:Path = 'C:\Program Files\OpenSSH-Win64;' + $env:Path
Get-Command ssh,scp
ssh -V
```

SSH, SCP and Git pushes use that session's `PATH`. The scripts do not select an
installation automatically or compensate for older Windows OpenSSH behavior.

The ignored `deploy/private/` directory contains the settings module, SSH private
key, verified `known_hosts`, `state.json` resource receipt and generated database
and session secrets. Keep these files for the lifetime of the host. The default
state directory is `deploy/private/NAME`; use a separate directory for a different
name, region, hostname or AWS account. Existing version 1 receipts and SSH keys
remain usable.

`create-key` generates the local RSA key if absent and imports its public key to
Lightsail. The SSH private key receives restrictive OpenSSH permissions;
the settings and state directory retain normal inherited filesystem permissions.
`setup` generates application secrets once and reuses them on later runs.
`trust-host` obtains SSH host keys through authenticated AWS and writes
`known_hosts`; SSH host checking stays enabled.

Database TLS uses a separate, deployment-specific private CA. Generate it before
creating the machine if desired:

```text
uv run --project deploy python deploy/manage.py create-certs
```

Python generates `ca`, `server`, `postgres` and `dl_importer` certificate/key pairs
under `STATE_DIR/tls`, restricted to the current OS user. Certificates have no
IP/DNS subject alternative names: clients explicitly use `sslmode=verify-ca` to
validate the deployment CA without hostname matching. An address is needed only
when connecting. This is independent of the website hostname and its HTTPS/DNS
setup. See [PostgreSQL's TLS modes](https://www.postgresql.org/docs/16/libpq-ssl.html).
The server certificate has server-auth usage; the client certificates have
client-auth usage and common names matching their database roles.

Repeated `create-certs` and `setup` validate and reuse existing material. Partial,
mismatched or expired bundles are rejected. The CA key and client private keys
stay on the workstation; only `server.key`, `server.crt` and public `ca.crt` go to
the host. `STATE_DIR/tls-ca.sha256` pins the original CA even if the TLS directory
is lost; preserve it with the certificate backup. Host setup also refuses to
replace an already installed CA. Keep the CA dedicated to this deployment because `verify-ca` trusts
server certificates issued by that CA regardless of their names.

## Check and inspect the runbook

```text
uv sync --project deploy --locked
uv run --project deploy python deploy/manage.py check
uv run --project deploy python deploy/manage.py steps
```

`check` validates all local inputs and tools without calling AWS. Other commands
validate the inputs they consume: Google settings and runtime pins for `setup`,
SSH CIDRs for `open-ports`, and catalog paths for `import-catalog`. Broken Google
or catalog settings do not block cloud inspection, deletion or diagnostic SSH.
`steps` prints the sequence without loading settings or executing it. If the
default uv cache is unavailable, add `--cache-dir deploy/.uv-cache` to uv commands.
To use another input module, put `--config PATH` before the action.

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

`open-ports` updates exact TCP 22/80/443 rules for the configured SSH CIDRs and
public HTTP/HTTPS, preserving other live firewall rules, including TCP 5432.
It needs no SSH connection, so it can restore access after your public IP changes.
It stops if a TCP range or all-protocol rule overlaps those ports; split the rule
in Lightsail while preserving its other intended access, then retry. Avoid
concurrent firewall edits because reading and replacing the rules is not atomic.

`cloud-status` reads AWS and shows the recorded resources and IP. If a command
fails, inspect its output and `cloud-status`. For an uncertain outcome or a
resource conflict, follow [cloud recovery](#recover-an-interrupted-cloud-step)
before retrying. Do not rerun all creation commands to resume. `trust-host` may
need a retry once AWS reports the host keys. Keep the receipt and key; deleting
them does not resolve a resource conflict.

## Install the host and first release

For an existing machine, start with the needed host step. `setup` uses the saved
`state.json`, SSH key and `known_hosts` without contacting AWS.
Run `trust-host` only if `known_hosts` is absent or a replacement host needs its
new key verified through AWS.

```text
uv run --project deploy python deploy/manage.py setup
uv run --project deploy python deploy/manage.py open-db-port
uv run --project deploy python deploy/manage.py verify-db
uv run --project deploy python deploy/manage.py prepare
uv run --project deploy python deploy/manage.py init-db
uv run --project deploy python deploy/manage.py deploy
uv run --project deploy python deploy/manage.py import-catalog
```

These are separate steps:

1. `setup` checks the SSH connection and waits for Ubuntu first boot, copies the
   readable scripts in `deploy/host/`, and runs `bootstrap.py` to install host
   services and runtimes, and install PostgreSQL's mTLS configuration. It generates
   the certificates if absent. PostgreSQL listens on standard TCP port `5432`; external access
   requires certificates for `postgres` or `dl_importer`. The local gateway keeps
   its existing password and continues using `127.0.0.1:5432`. Startup migrations
   use a separate connection as `dl_importer` over the local Unix socket, with
   peer authentication mapping only the application's OS account to that role.
   Setup writes `MIGRATION_DATABASE_URL` to the application environment and
   manages both `pg_hba.conf` and `pg_ident.conf`. Applying PostgreSQL configuration
   changes restarts PostgreSQL, causing a brief database interruption.
2. `open-db-port` verifies the running TLS settings and authentication rules over
   verified SSH, then adds permanent public IPv4 TCP `5432` access in Lightsail.
   It changes only the database rule. No local firewall state is saved.
3. `verify-db` tests trusted mTLS access and rejection of missing/untrusted client
   certificates directly from the deployment environment. It requires no backend
   checkout, revision or clean application working tree.
4. `prepare` pushes a Git revision and installs/builds its locked dependencies.
5. `init-db` uses the local backend environment and bootstrap `postgres`
   certificate to create the fresh database, initialize its schema, and create
   `dl_importer`. The importer can assume `dl_owner` (schema-owner privileges)
   but has no PostgreSQL superuser, role-creation or database-creation privileges.
6. `deploy` starts the candidate, which applies pending migrations as `dl_owner`
   and closes its privileged connection before opening the ordinary gateway pool.
   It checks readiness, switches Caddy and retires the previous release. It
   prepares the selected revision if needed. A migration failure prevents the
   candidate from becoming ready and leaves the old release active.
7. `import-catalog` reads the local CSV, referenced images, and optional
   `CATALOG_DETAILS` JSON and writes directly through psycopg with the importer
   certificate. Set `CATALOG_DETAILS = "../../pokemon_details.json"` in
   `deploy/private/settings.py` to import types, abilities, base stats, Mega
   Evolutions, and learned moves. Run after `deploy` so the required tables exist.
   There is no archive upload or remote importer process.

Before deploying startup migrations to an existing host, rerun `setup` to install
the local peer-authentication mapping and add `MIGRATION_DATABASE_URL`. This does
not reinitialize the database. The first migration, `0001_tournament.sql`, adds
tournament support at startup. The schema and grants used by `init-db` remain
the fixed baseline.

Commit the intended app changes before preparing a release. The default revision
is `HEAD`; use `--revision FULL_COMMIT_ID` on `prepare`, `init-db`, `import-catalog`
and `deploy` to pin the same source revision across steps. These commands refuse
dirty application/deployment directories by default. Add `--allow-dirty` to each
if intentionally deploying the selected **committed** revision while local edits
exist; uncommitted app files are excluded. `setup` uses the local `deploy/host/`
scripts, so changes to the installer do not require an application commit.
The selected backend commit must contain `scripts/deployment_db.py`. `init-db`
and `import-catalog` extract that exact backend revision locally and prepare its
locked dependencies with uv; neither requires a prepared remote release.
`verify-db` accepts neither `--revision` nor `--allow-dirty`.

`setup`, `install-db-certs`, release commands, `status`, `ssh` and `logs` use the
saved receipt and verified host keys; they do not call AWS. `open-db-port` is the
explicit SSH/AWS step. `init-db`, `import-catalog` and `verify-db` use direct
PostgreSQL connections without SSH or AWS. They default to the saved IP; pass
`--host ADDRESS` to use an explicit database address without a receipt. Routine
imports need only the public CA, importer certificate/key, local inputs and
selected backend revision; initialization additionally needs the saved gateway
password in `secrets.json`.

Commands print their nonsecret command lines and stream installation/build output.
Short values needed by the scripts, such as the upload-directory path, are
captured. Long workstation waits print elapsed progress every 15 seconds and
have finite time limits. `status: done` from `cloud-init` means only that Ubuntu's
first boot has finished; the named host installation step follows.

Progress is plain text with new lines, without spinners or ANSI animation. SSH
uses a 15-second connection timeout. The runner limits the initial SSH check to
30 seconds, the first-boot wait to 10 minutes, and host installation to 30 minutes,
with Ctrl-C cancellation.

`Completed: Checking SSH` means authentication and host-key verification both
succeeded. The next step is `sudo -n cloud-init status --wait`. The deployment
uses `deploy/private/NAME/known_hosts`; plain `ssh -i ...` uses your user's default
known-hosts file and may therefore ask you to trust the same server separately.
Use `manage.py ssh` to connect with the deployment's saved trust settings.

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

Deploying code applies schema migrations but does not populate catalog metadata.
After deploying changes to `pokemon_details.json`, run `import-catalog` with
`CATALOG_DETAILS` configured and the same committed backend revision. Without that
setting, imports update the CSV fields and images while preserving existing
details; they do not add any new battle details or moves. The selected revision
must support the `--details` deployment import option.

```text
uv run --project deploy python deploy/manage.py import-catalog --revision FULL_COMMIT_ID
```

Reload the page after importing to refetch the catalog. The Moves tab and search
suggestions are built from the imported data; empty move lists hide the tab.

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

Ordinary releases apply pending migrations during application startup; they never
initialize or reset the database. Each migration commits separately, so successful
migrations remain applied if a later one fails. On retry, startup resumes with
the pending migrations, and readiness still requires all of them to succeed. `init-db`
checks its administrator-owned completion record inside PostgreSQL and preserves
initialized data. Initialization and imports use database advisory locks. If
initialization stopped after creating the database but before committing that record, it refuses
the unmarked database; inspect the failure and rebuild the disposable host rather
than expecting an automatic reset. Catalog import requires completed initialization
and upserts without deleting omitted rows/assets. Code rollback does not undo
database changes.

Certificates last one year (the CA lasts ten years). Renewal is explicit and
retains the CA and identity keys:

```text
uv run --project deploy python deploy/manage.py renew-certs
uv run --project deploy python deploy/manage.py install-db-certs
uv run --project deploy python deploy/manage.py verify-db
```

`install-db-certs` uploads only `bootstrap.py`, `server.key`, `server.crt` and
public `ca.crt`; it needs no CA or client private keys. Under the host deployment
lock it validates the certificates, requires the installed CA to match, and
configures/restarts PostgreSQL as needed. It does not generate certificates,
require Google settings, or rerun package, runtime, application or Caddy setup.
A changed server certificate briefly interrupts database connections. Run
`setup` for the first installation.

Renewal does not silently replace an expired/missing CA and does not revoke old
certificates. Keep the private CA backed up; CA replacement or compromise needs
a separate coordinated trust change. Ordinary imports need no renewal or host setup.

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

## Recover an interrupted cloud step

AWS may finish a request whose response or receipt update was lost. Stop other
cloud commands and console edits while inspecting it. Keep the printed request,
operation ID and time. Use the AWS CLI for the following inspection, with your
actual `STATE_DIR` and profile; omit both `--profile` and `YOUR_PROFILE` when
using default credentials:

```powershell
$receiptPath = 'E:\bz\deploy\private\draft-preprod\state.json'
$receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
Copy-Item -LiteralPath $receiptPath -Destination "$receiptPath.before-repair-$(Get-Date -Format yyyyMMdd-HHmmss)"
$awsArgs = @('--region', $receipt.region, '--profile', 'YOUR_PROFILE', '--no-cli-pager')
aws @awsArgs sts get-caller-identity
aws @awsArgs lightsail get-key-pair --key-pair-name $receipt.key_pair_name
aws @awsArgs lightsail get-instance --instance-name $receipt.instance_name
aws @awsArgs lightsail get-static-ip --static-ip-name $receipt.static_ip_name
```

The STS account must equal `account_id`. Check resource names, ARNs, regions and
creation times. Recorded ARNs must match exactly. `NotFoundException` means
absent; a permissions or network error does not. Resources whose creation step
has not run will be absent.

Inspect the printed operation ID, or the affected resource's operation history
if no ID reached the workstation:

```powershell
aws @awsArgs lightsail get-operation --operation-id 'PRINTED_OPERATION_ID'
aws @awsArgs lightsail get-operations-for-resource --resource-name 'RECORDED_RESOURCE_NAME'
```

Follow pagination tokens and match the request time, operation type, resource
name and parameters. Wait for pending operations. For key imports or incomplete
operation history, inspect CloudTrail Event history in the same region. Filter
by the request's time window and API event (`ImportKeyPair`, `CreateInstances`,
`AllocateStaticIp`, `AttachStaticIp`, `DeleteInstance`, `ReleaseStaticIp` or
`DeleteKeyPair`). Confirm the caller, account, parameters, outcome and any later
deletion or recreation of that name. See
[Lightsail audit events](https://docs.aws.amazon.com/lightsail/latest/userguide/logging-lightsail-api-calls-using-aws-cloudtrail.html).

For creation recovery, a matching name is insufficient. Compare the instance's
`sshKeyName`, blueprint, bundle and location with the printed request. For an
imported key, compare the audit event's submitted public key with `id_rsa.pub`,
and confirm it comes from the saved private key using
`ssh-keygen -y -f PATH_TO_ID_RSA`. Compare the public file's SHA-256 with
`key_sha256`:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath ([IO.Path]::ChangeExtension($receipt.private_key, 'pub'))
```

Once identity and outcome are established, edit `state.json` in a text editor:

| Interrupted step | Confirmed outcome | Receipt repair and next action |
| --- | --- | --- |
| `create-key`, `create-instance`, `allocate-ip` | The current resource was created by this request | Add its exact AWS `arn` alongside `name` in `resources.key_pair`, `resources.instance` or `resources.static_ip`. Continue with the next step. |
| Those creation steps | Request failed, resource is absent, receipt entry contains only `name` | Leave the receipt unchanged. Fix the cause and rerun that creation step. |
| `attach-ip` | Both ARNs match; `isAttached` is true; `attachedTo` equals the recorded instance name; its `publicIpAddress` equals the static IP's `ipAddress` | Set top-level `ip` to that `ipAddress`. Continue with `trust-host`. |
| `attach-ip` | Operation finished and the recorded static IP remains unattached | Leave the receipt unchanged and rerun `attach-ip`. Investigate an attachment to any other instance separately. |
| `delete-instance`, `release-ip`, `delete-key` | Original deletion/release completed and the resource is absent | Remove only its object from `resources`. Also remove top-level `ip` after instance deletion or IP release. Continue with the next deletion step. |
| Those deletion steps | Operation failed and the resource retains its recorded ARN | Leave the receipt unchanged, fix the cause and rerun that deletion with the deployment-name confirmation. |

Keep the version, deployment/account/region fields, top-level resource names,
private-key path and key hash unchanged. Never substitute a same-named
replacement's ARN. If evidence is missing or contradictory, resolve ownership
in AWS before editing. Preserve keys, TLS material, secrets and `known_hosts`;
a replacement instance needs its host key separately verified by `trust-host`.
After repair, run `cloud-status`, confirm surviving resources have
`matches_receipt: true`, and compare the receipt with its backup.

Firewall recovery needs no receipt edit:

```powershell
aws @awsArgs lightsail get-instance-port-states --instance-name $receipt.instance_name
```

Inspect the live rules, then rerun `open-ports` or `open-db-port` as needed.
`open-ports` preserves a live database rule and never recreates one from the
obsolete `database_port_open` receipt field; that field is ignored and can be
removed. `open-db-port` still verifies host TLS before adding TCP 5432.

## Verification and implementation

```text
uv run --project deploy python -m pytest deploy/tests -q
uv run --project deploy ruff check deploy
uv run --project draft_backend python -m pytest draft_backend/tests/test_deployment_db.py -q
```

Local tests use fake AWS clients and host commands, plus in-memory TLS handshakes.
They do not provision or contact a server. The optional
`draft_backend/tests/test_deployment_tls_integration.py` suite starts a fresh local
PostgreSQL cluster to exercise trusted and rejected certificates, initialization,
imports and operation locks. Run it from `draft_backend` with
`uv run python -m pytest tests/test_deployment_tls_integration.py --integration --pg-bin PATH -q`.
Live acceptance still needs a fresh installation, Google login,
two-browser draft changes, a schema-compatible update and rollback, a failed
candidate, and service/reboot recovery. Check that data and sessions survive the
release changes, and that the saved release returns after reboot. Brief failures
during crashes/reboot are expected on this single host.

Read `cloud.py` for individual AWS actions, `remote.py` for SSH/copy commands, and
`host/bootstrap.py` and `host/release.py` for the scripts run on the machine.
`tls.py` manages certificates, `database.py` verifies transport and selects the
local backend environment, and `draft_backend/scripts/deployment_db.py` owns
database initialization and import.
The release handoff follows
[the reference spec](../caddy-uv-systemd-deployment.md). No automatic release or
shared-asset pruning or backups are included. Database migrations run automatically
before application readiness.
