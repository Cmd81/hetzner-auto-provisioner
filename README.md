# Hetzner Cloud Capacity Watcher (Ubuntu 22.04)

A lightweight Python (standard-library only) service that watches Hetzner Cloud capacity by **server type and location**, then optionally places a **limited number of server orders** when stock appears.

**Default image:** `ubuntu-22.04` (Ubuntu Server 22.04 LTS). **Default check interval:** 60 seconds. **Default purchase mode:** OFF (`AUTO_CREATE=false`).

## Features

- Watches multiple `TYPE@LOCATION=COUNT` targets in one `.env` configuration.
- Uses **`GET /v1/server_types` → `locations[].available`**, not the removed Data Centers availability endpoint.
- Creates servers with `POST /v1/servers` using `server_type`, `location`, `image`, and optional pre-registered SSH keys.
- Follows asynchronous Hetzner actions before marking a server as successfully provisioned.
- Prevents accidental repeat orders using persistent completed/pending state and stable server names.
- Caps the number of orders per check cycle and automatically backs off on HTTP 429.
- Runs as an unprivileged `systemd` service, enabled on boot.
- **Never deletes servers.** Reducing a count does not delete previously purchased servers.

## One-line install (Ubuntu / Debian with systemd)

Review [`install.sh`](./install.sh) before running scripts with `sudo`:

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/Cmd81/hetzner-auto-provisioner/main/install.sh)"
```

The installer downloads `monitor.py` and `.env.example` from the same GitHub folder, sets up a restricted service account, writes the `systemd` unit, and starts the watcher after you provide a Hetzner Cloud token. The token is read without terminal echo, and is kept only in the root-owned config file (mode `0600`), **never in GitHub**.

If you skip the token prompt, configure `/etc/hetzner-auto-provisioner.env` later and start the service manually.

## Configure

```bash
sudo nano /etc/hetzner-auto-provisioner.env
```

Example:

```dotenv
HETZNER_API_TOKEN=YOUR_HETZNER_TOKEN
POLL_INTERVAL_SECONDS=60
SERVER_IMAGE=ubuntu-22.04
TARGETS=cx23@fsn1=1,cx23@nbg1=2,cpx22@hel1=1
NAME_PREFIX=hcloud-auto
SSH_KEYS=
AUTO_CREATE=false
MAX_CREATES_PER_CYCLE=1
STATE_FILE=/var/lib/hetzner-auto-provisioner/state.json
```

### Meaning of `TARGETS`

Each entry is `SERVER_TYPE@LOCATION=MAXIMUM_NUMBER`:

| Entry | Meaning |
|---|---|
| `cx23@fsn1=1` | Provision up to **one CX23** in Falkenstein |
| `cx23@nbg1=2` | Provision up to **two CX23** in Nuremberg |
| `cpx22@hel1=1` | Provision up to **one CPX22** in Helsinki |

Add or remove comma-separated entries freely; count must be 1–20 per entry. The script doesn't delete a server if you decrease count or remove the entry. `SERVER_IMAGE` applies to all targets; default is Ubuntu 22.04 LTS. `SSH_KEYS` is a comma-separated list of **existing** Hetzner project SSH key names or numeric IDs (recommended); if empty, Hetzner may deliver a temporary root password through its normal mechanisms.

**Costs:** Each successful server creation is billable according to your Hetzner account. `AUTO_CREATE=false` logs available stock without placing any order. Once you're ready, set `AUTO_CREATE=true` and restart the service. `MAX_CREATES_PER_CYCLE=1` is an additional safety cap; a higher quantity is requested over multiple polling rounds.

### Apply changes

```bash
sudo systemctl restart hetzner-auto-provisioner
sudo systemctl status hetzner-auto-provisioner
sudo journalctl -u hetzner-auto-provisioner -f
```

Pause / resume:

```bash
sudo systemctl stop hetzner-auto-provisioner
sudo systemctl start hetzner-auto-provisioner
```

## Behavior and safeguards

- A target is considered provisioned only when the create action returns success (or a previously-created managed server is found stable during recovery). `state.json` remembers successfully filled slots **even if you manually delete the servers**. This prevents repeat charges: the app is an *order-when-available tool*, not an auto-healing server fleet manager.
- **To intentionally order again** after completed slots are filled: stop the service, clear the relevant names under `completed` in `/var/lib/hetzner-auto-provisioner/state.json` (or use a new unique `NAME_PREFIX`), then start the service. Verify existing server names first to avoid conflicts.
- Editing `TARGETS` does not make the script delete, downsize, or migrate an existing server.
- Server names are deterministic: `PREFIX-TYPE-LOCATION-NN` (e.g. `hcloud-auto-cx23-fsn1-01`), and creations get a `managed-by=hetzner-capacity-watcher` label.
- Pending HTTP requests that fail/timeout are not automatically retried *in-place*. On the next poll, the script lists servers to reconcile first. Rare API propagation delays could still cause ambiguity; check Hetzner Console on any unexpected timeouts.
- Hetzner can show `available=true` and later reject provisioning or fail the asynchronous action when supply is tight. The watcher keeps monitoring in that case. It cannot bypass account/order limits.
- Rate limit defaults to **3,600 API requests/hour/project**, but other consumers of your token/project share the quota. A 60-second poll is generally conservative (a normal cycle uses server-types + servers list requests); 429 triggers cooldown based on headers.
- Token must have **Read & Write** permissions; don't commit `.env`, email your token, or paste it into a public issue.

## Manual use / testing

On Linux with Python 3.9+:

```bash
cp .env.example .env
nano .env
python3 monitor.py --once
python3 -m unittest discover -s tests -v
```

For a checkout-based installation: `sudo bash install.sh`. The installer preserves any existing `/etc/hetzner-auto-provisioner.env` on reinstallation. Re-run it to update the installed script from the GitHub folder.

### Official references

- [Hetzner Cloud API](https://docs.hetzner.cloud/reference/cloud)
- [Hetzner API changelog — per-location availability](https://docs.hetzner.cloud/changelog)
- [Hetzner FAQ: stock availability is not a guarantee](https://docs.hetzner.com/cloud/servers/faq/)
