# Lightsail deployment

This directory owns the disposable preproduction deployment. It creates one
Ubuntu 24.04 x86-64 Lightsail instance with PostgreSQL 16, Caddy, and one running
backend release. Caddy serves the built React frontend and proxies the API,
authentication, images, readiness, and event streams through a Unix socket.
DNS changes and cleanup of ambiguous provisioning failures are manual. Releases
retain blue-green handoff: the old service serves traffic until the candidate is ready.

The administrator runs these commands with their own AWS credentials. Preparing
or checking this code does not provision infrastructure. The implementation has
local tests; the live acceptance exercises below still need to be run on Lightsail.

## One Python input module

Copy `settings.example.py` to **`deploy/private/settings.py`** and edit it. The
entire `private/` directory is gitignored. It holds the input module, SSH key,
verified SSH host keys, resource manifest, and generated application secrets.
Keep it until the deployment has been torn down.

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
selected from the registered HTTPS redirect URI for `HOSTNAME`. If several match,
set `OIDC_CALLBACK_PATH` explicitly. The supplied client file uses `/g`; the
backend and Caddy now support that path. To use a different hostname, register its
exact callback URI in Google and update the JSON. Credentials alone do not change
Google's server-side registration. A Google consent screen in testing mode also
needs the intended accounts configured as test users in Google Cloud.

The other inputs are documented in the example: optional AWS profile and
Availability Zone, instance bundle, catalog CSV and image directory, and pinned
uv/Node versions. Relative paths resolve against the settings module's directory.
Region and hostname are inputs throughout. The installer deliberately targets
Ubuntu 24.04 x86-64; changing the OS requires an installer change.

Python generates a 3072-bit RSA SSH key locally on the first provisioning run and
imports only its public key into Lightsail. RSA is supported by Lightsail's key
import API. The private key is retained on retries and kept in the ignored state
directory. The directory and editable settings use normal inherited filesystem
permissions. Only the SSH private key receives the restrictive permissions that
OpenSSH expects. AWS's authenticated host-key information populates `known_hosts`;
SSH host checking remains enabled.

The state directory defaults to `deploy/private/NAME`. Changing `NAME`, `REGION`,
or `HOSTNAME` describes a different deployment; use a separate state directory
when changing those inputs. Do not reuse a manifest for another host/account.

## Prerequisites

- uv, Git, and OpenSSH (`ssh` and `scp`) on the workstation. No local Node or
  PostgreSQL installation is needed for deployment.
- An authenticated AWS profile/credential chain. Credentials remain on the
  workstation; do not put IAM access keys into the settings module.
- An existing registered domain and authoritative DNS zone, and the Google client.
- The approved catalog CSV and referenced PNGs. Runtime images are stored in
  PostgreSQL, so the original image tree is needed only during import.

The AWS identity needs the Lightsail get/list, key import/delete, instance
create/delete, static IP allocate/attach/detach/release, firewall update,
operation-status, and instance-access-details actions used by `cloud.py`, plus
`sts:GetCallerIdentity`. The scripts do not access Route 53. Existing IAM policies
may constrain regions or resource names. The scripts do not change IAM permissions.

## First bring-up

Run from the repository root in PowerShell or a POSIX shell:

```text
uv sync --project deploy --locked
uv run --project deploy python deploy/manage.py check
```

`check` is local only. It validates the inputs and reports the selected callback
without displaying credentials. If the default uv cache is unavailable, add
`--cache-dir deploy/.uv-cache` to uv commands.

Commit the intended backend, frontend, and deployment files before deploying.
The release contains the selected Git commit's `draft_backend/` and
`draft_frontend/`, including their lockfiles. Existing uncommitted work is not
included. The default refuses dirty app/deploy directories; `--allow-dirty` is an
explicit opt-in to deploy the selected committed revision anyway. Host tooling
is installed from the local `deploy/host/` files.

```text
uv run --project deploy python deploy/manage.py bringup
```

Bring-up provisions the instance and network, waits for SSH/cloud-init, installs
host software, pushes and builds the chosen commit, initializes the fresh
database, imports the catalog, activates the application, then prints the IP for
the manual DNS change. It leaves the previous site serving while the new host is
prepared. Save the current DNS record yourself, then set the hostname's A record
to the printed IP. Remove or migrate any AAAA/CNAME that would send clients
elsewhere. Existing DNS caches can continue using the previous IP until their
TTL expires. Allow DNS propagation and certificate issuance before:

```text
uv run --project deploy python deploy/manage.py smoke
```

DNS records are neither checked nor stored nor restored by these commands. If
upgrading an older settings module, remove a configured `HOSTED_ZONE_ID`; DNS is
now always manual.

The same workflow is available in separate steps for the first exercise:

```text
uv run --project deploy python deploy/manage.py provision
uv run --project deploy python deploy/manage.py cloud-status
uv run --project deploy python deploy/manage.py setup
uv run --project deploy python deploy/manage.py prepare
uv run --project deploy python deploy/manage.py init-db
uv run --project deploy python deploy/manage.py import-catalog
uv run --project deploy python deploy/manage.py deploy
# Manually set the DNS A record to the IP shown by cloud-status.
uv run --project deploy python deploy/manage.py smoke
```

Use `--config PATH` before the action to select another settings module.
Use `--revision FULL_COMMIT_ID` on prepare/init-db/import-catalog/
deploy/rollback to keep every step tied to the same source revision.

## Releases and operations

```text
uv run --project deploy python deploy/manage.py deploy
uv run --project deploy python deploy/manage.py rollback --revision FULL_COMMIT_ID
uv run --project deploy python deploy/manage.py status
uv run --project deploy python deploy/manage.py logs app --revision FULL_COMMIT_ID
uv run --project deploy python deploy/manage.py logs caddy
uv run --project deploy python deploy/manage.py logs postgres
uv run --project deploy python deploy/manage.py ssh
```

Deployment builds with `npm ci` and the locked backend environment as the
unprivileged application account. Builds happen once per release, before traffic
switches. Old hashed frontend assets remain available in `/srv/NAME/assets`.
Each backend has its own virtual environment and Unix socket; the uv cache is
shared. The host uses the existing backend initializer and importer, rather than
duplicating the application's schema logic.

Dependency installation/build and candidate startup happen while the old release
continues serving. Preparation can take an unbounded time; candidate readiness
has a 180-second timeout. A failure before handoff leaves the old route and
service in place. This is why separate release services are retained: replacing
them with one service and a `current` pointer would stop the old process before
the new one is ready, making startup delays or failure an outage.

After readiness, the candidate service is enabled for boot. One Caddy API load
switches the frontend root and backend socket. Only after that configuration has
also been saved for reboot are previous services stopped. Existing sessions use
the same secret across release changes. Event streams can reconnect during
handoff; open browsers can continue using their existing frontend until refreshed.
Keep adjacent releases' API and database contracts compatible.

`status` shows live and saved routing and whether they agree. If a deployment
was interrupted after Caddy accepted a change but before persistence, both
services remain. Repeating `deploy --revision ...` reconciles the route and
completes cleanup. A failed candidate remains available for log inspection and
may continue restarting; stop it explicitly if abandoning it:

```sh
# On the server, substitute NAME and the full commit ID:
sudo systemctl disable --now NAME@COMMIT.service
```

There is no database migration runner. Ordinary deployment never initializes,
resets, or migrates the database. Rolling back code does not undo database changes.
Re-running `init-db` checks the bootstrap completion marker and preserves data.
Catalog import requires that same marker. These commands do not repeatedly audit
roles, grants, or tables; successful bootstrap establishes them, and application
readiness gates activation. If first initialization was interrupted after
creating the database but before writing its completion marker, it refuses the
unmarked state. Inspect the failure and rebuild the disposable host; it never
silently deletes that database. Catalog import upserts and preserves IDs; omitted
rows/assets remain, matching the existing importer.

Successful provisioning can be rerun using the recorded resources. Incomplete
provisioning must be cleaned up before retrying; the scripts do not replay saved
operations or resume a partially created deployment. Keep `state.json` and the
SSH key. Inspect the resources named in the manifest using the Lightsail console,
and wait for any pending AWS operation to settle. `cloud-status` can check
recorded resources, but refuses an existing resource whose ARN was not saved.

Run `destroy --confirm NAME` to remove resources with recorded identities. If a
creation lost its response before the resource ARN was saved, teardown refuses
to claim the resource by name: inspect and delete that resource manually in the
correct AWS account and region, then rerun `destroy` to finish. After cleanup,
run `bringup` again. Do not discard the manifest to bypass the incomplete state.
This manual cleanup policy concerns cloud provisioning; release handoff retains
its existing retry/reconciliation behavior.

## First acceptance exercise

Record the instance IP and both tested commit IDs with your results.

| Exercise | Actions | Expected result |
|---|---|---|
| Fresh installation | Run bring-up and smoke; sign in with Google; view catalog/images; create a league and draft in two browser sessions | Valid HTTPS, `/g` completes sign-in, writes succeed, second browser updates |
| Update and rollback | Deploy a second schema-compatible commit while signed in; make a draft change; roll back by SHA | Frontend/backend select the intended release, sessions remain valid, SSE reconnects, data remains |
| Failed candidate | Deploy a deliberately broken startup commit; inspect status/logs; deploy the known-good SHA again | Failure occurs before traffic switch; existing release remains usable; retry restores agreement |
| Process and host recovery | On the server kill the active app process; restart PostgreSQL; finally reboot the instance | systemd restarts the app, listeners reconnect, saved release returns after reboot, draft data remains |
| Recreate | Tear down, then bring up again with the same input module | New instance/database, successful import/sign-in, old browser cookies invalidated |

Open a server shell with the `ssh` action above. For the recovery exercise,
commands on the server are:

```sh
sudo systemctl kill --signal=SIGKILL --kill-whom=all NAME@COMMIT.service
sudo systemctl restart postgresql@16-main.service
sudo reboot
```

Check service readiness between these actions. Brief request failures during
crashes/reboot are expected. This is one host, with its database on local storage;
destroying it discards application data. Backups/restores and migration policy
can be added after this disposable deployment is exercised.

## Teardown

```text
uv run --project deploy python deploy/manage.py destroy --confirm draft-preprod
```

Use your exact `NAME` as the confirmation. Restore or remove the DNS A record
yourself before teardown; allow cached records to expire if clients must finish
using the old host. Teardown deletes the owned instance, releases
the static IP, and deletes the imported Lightsail key pair. It leaves the local
input module, reusable private key, and final manifest. Application/database
secrets are removed locally, so the next bring-up generates new ones. Resources
from earlier experiments or other manifests are not discovered or deleted.

## Local checks

```text
uv run --project deploy python -m pytest deploy/tests -q
uv run --project deploy ruff check deploy
```

Tests use fake AWS clients and mocked host commands: they do not authenticate to
AWS, SSH to a server, or install host services. They cover credential/key reuse,
ownership checks, manual cleanup boundaries, teardown, Caddy handoff failures,
asset retention, database initialization boundaries, and safe configuration/archives. Backend
tests cover Unix-socket startup settings, readiness, and configurable callbacks.
Real Linux services, certificate issuance, Google consent settings, AWS policy,
and browser behavior are verified by the acceptance exercise, not these tests.

## Design and upstream references

The release lifecycle adapts [the reference spec](../caddy-uv-systemd-deployment.md).
Administration scripts remain outside application releases; they are installed
by `setup`, while normal release commands operate on immutable committed source.
No automatic release or shared-asset pruning is included initially.

- [Lightsail Boto3 API](https://docs.aws.amazon.com/boto3/latest/reference/services/lightsail.html)
- [Lightsail SSH key import](https://docs.aws.amazon.com/boto3/latest/reference/services/lightsail/client/import_key_pair.html)
- [Caddy installation](https://caddyserver.com/docs/install) and [configuration API](https://caddyserver.com/docs/api)
- [Google OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect)
- [Pinned Node release](https://nodejs.org/en/blog/release/v24.22.0)
- [Pinned uv release](https://github.com/astral-sh/uv/releases/tag/0.12.20)
