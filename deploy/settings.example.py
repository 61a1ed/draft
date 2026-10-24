"""Copy to private/settings.py and fill in these deployment inputs.

This is trusted Python code. Keep the configured module and generated private/
directory out of Git. Relative file paths are relative to this settings file.
"""

NAME = "draft-preprod"
REGION = "us-west-2"
AVAILABILITY_ZONE = None  # None uses REGION + "a", for example "us-west-2a".
AWS_PROFILE = None  # None uses your normal AWS credential chain.
BLUEPRINT_ID = "ubuntu_24_04"
BUNDLE_ID = "small_3_0"  # Lightsail bundle ID; choose a Linux bundle with at least 2 GB RAM.
HOSTNAME = "slaprat.click"
SSH_CIDRS = []  # Required: administrator's public IPv4 address, e.g. ["203.0.113.10/32"].
# DNS is manual: point HOSTNAME's A record at the IP shown by cloud-status.

# Paste the entire downloaded Google *web application* client JSON here.
# Include https://HOSTNAME/<callback-path> in its registered redirect_uris.
GOOGLE_OIDC_JSON = r"""{
  "web": {
    "client_id": "REPLACE.apps.googleusercontent.com",
    "client_secret": "REPLACE",
    "redirect_uris": ["https://slaprat.click/auth/callback"]
  }
}"""
# OIDC_CALLBACK_PATH = "/auth/callback"  # Only needed if several URIs match HOSTNAME.

CATALOG_CSV = "../../pokemon_catalog.csv"
IMAGE_ROOT = "../../showdown"
CATALOG_DETAILS = "../../pokemon_details.json"  # Types, abilities, stats, Megas, and moves.
# STATE_DIR = "./draft-preprod"  # Default: deploy/private/NAME; keep it gitignored.
UV_VERSION = "0.12.20"
NODE_VERSION = "24.22.0"
