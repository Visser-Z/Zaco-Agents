# Deploying Zacon

Zacon is an internal system. It is **not** meant to be reachable from the public
internet. Access is controlled at the network layer with a Tailscale tailnet, so
only approved devices can reach it at all — there is no public port to find.

> **Not suitable: Vercel, Netlify, and similar.** They are public-by-default
> edge platforms with ephemeral filesystems, so the product-code lookup would
> not persist and the app could not be placed on a tailnet.

---

## 1. Order the VPS

Hosted on a **South African VPS**. The users are in Pretoria, so a local server
answers in roughly 10ms instead of the ~170ms a European host would add to every
click, upload and save. It also bills in rands.

Provider: **Xneelo Cloud** (their VPS product — *not* "Servers", which is
dedicated hardware and heavily oversized for this).

**What to order:**

| | |
|---|---|
| OS | **Ubuntu 24.04 LTS** |
| Flavour | **`s-g-1cpu-2gb`** — 1 vCPU / 2 GB |
| Boot volume | 10 GB premium (NVMe), included by default |
| Access | SSH key preferred over a root password |

**Cost (as quoted July 2026):**

| Item | Rand/month |
|---|---|
| Compute `s-g-1cpu-2gb`, incl. 10 GB premium boot volume | 120.45 |
| Floating IP (IPv4) | 0 while promotional, **51.10 thereafter** |
| Volume snapshot for backups (optional, recommended) | ~12 |
| **Total** | **≈ 120 now, ≈ 172 once the IP is charged** |

Prices include VAT. No extra Cloud Storage volume is needed — the boot volume
that comes with the instance is enough to start, and de-coupled storage means
one can be added later without a rebuild.

**The floating IP is only needed for setup.** It provides the public address used
to SSH in and install Tailscale. Once the tailnet is up, both the app and SSH
work over it and the public IP is idle — so if it stops being free, detaching it
saves R51/month *and* removes the public attack surface entirely. Before doing
that, confirm the provider's browser console works (for recovery if Tailscale
fails) and that outbound traffic still works without it, so `apt` keeps running.

### Sizing and the upgrade path

2 GB comfortably runs the app as it stands. The pressure point is Postgres:
when storage moves off Excel and onto a database, step up to `s-g-2cpu-4gb`
(~R198/month compute, ≈R230 all-in).

Upgrading is straightforward because compute is billed hourly and storage is
**de-coupled** — the boot volume is a separate resource, so resizing the
instance keeps the disk and its contents. Expect a short reboot, not a rebuild.

Disk can also be grown later without touching the instance, so there is no
reason to over-provision it now. 10 GB lands around 50-60% used once Ubuntu,
the virtualenv and Postgres are on it.

**On 1 vCPU, do not run multiple uvicorn workers.** One worker is correct here;
extra workers on a single core add memory pressure and contention for no gain.

**When it is provisioned you will have:** a floating IP and the key pair you
imported. That is everything needed to log in.

> The public IP is only used for the initial SSH setup. Once Tailscale is
> running, the app itself is never served over it.

---

## 1b. First login and basic hardening

Generate the key **on your own machine** and give the provider only the public
half. A key generated in a provider's web console is shown once and cannot be
re-issued if the download is missed.

```powershell
mkdir -Force C:\Users\vanzy\.ssh
ssh-keygen -t ed25519 -f C:\Users\vanzy\.ssh\zacon -C "zacon"
Get-Content C:\Users\vanzy\.ssh\zacon.pub    # paste this into "Import Public Key"
```

Then log in. Ubuntu cloud images disable direct root login — the default user is
**`ubuntu`**, which already has sudo, so there is no need to create another one:

```powershell
ssh -i C:\Users\vanzy\.ssh\zacon ubuntu@<floating-ip>
```

> The floating IP changes if the instance is recreated. `ssh-keygen -R <old-ip>`
> clears the stale host key. Once Tailscale is running this stops mattering —
> the machine is reachable by name regardless of its public IP.

Then on the server:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git
```

**Add a swap file.** On a 2 GB instance this is not optional. Installing the
dependencies (pdfplumber pulls in pillow and cryptography) and the occasional
processing spike can otherwise exhaust RAM, and Linux responds by killing the
process — usually the app, sometimes mid-request.

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab   # survives reboot
free -h                                                       # confirm swap is listed
```

`sudo tee -a` rather than `>>`: a plain redirect is performed by the shell as the
logged-in user, so it fails on a root-owned file even under `sudo`.

Swap is slower than RAM, so this is a safety net rather than a substitute for
memory — but it turns "the process died" into "that request was briefly slow",
which on a 2 GB box is a trade worth making.

---

## 2. Put the host and the laptops on a tailnet

1. Create a free account at <https://tailscale.com>.
2. Install Tailscale **on the host** and sign in.
   - Linux: `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`
   - Windows: install the app, sign in.
3. Install Tailscale **on each approved laptop** and sign in with the same account.
4. Note the host's tailnet address (`tailscale ip -4`) — it looks like `100.x.y.z`.
   Its machine name (e.g. `zacon`) also resolves inside the tailnet.

Approved laptops now reach the host. Nothing else on the internet can.

**Managing access:** add a laptop by installing Tailscale and signing in; remove
one by deleting the device in the Tailscale admin console. Access is per device,
so it keeps working when someone moves between office, home, or a hotspot.

---

## 3. Get the code onto the server and install it

**Option A — private Git repo (recommended).** Push the project to a private
GitHub repo, then on the server:

```bash
git clone https://github.com/<you>/zacon.git /opt/zacon
```

Updating later is then just `git pull && sudo systemctl restart zacon`.

**Option B — copy directly.** From your Windows machine:

```powershell
scp -r C:\Users\vanzy\Desktop\Zacon ubuntu@<floating-ip>:/opt/zacon
```

Simple, but every update is a manual re-copy with no history. Fine to start,
worth replacing with Option A once the system is in daily use.

**Then install into a virtualenv** (keeps Zacon's packages away from the
system Python, which Ubuntu is fussy about):

```bash
cd /opt/zacon/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q      # 16 tests should pass
```

Running the tests here is a real check: it proves Python, the dependencies and
the file layout are all correct before you involve the network.

Bind the app to the tailnet address so it never listens on a public interface:

```bash
export ZACON_ALLOWED_NETWORKS="100.64.0.0/10,127.0.0.0/8"
.venv/bin/python -m uvicorn app.main:app --host "$(tailscale ip -4)" --port 8000
```

Then open **http://\<host-tailnet-ip\>:8000** (or `http://zacon:8000`) from an
approved laptop.

### Why both a bind address and an allowlist

Binding to the tailnet address is the real control. `ZACON_ALLOWED_NETWORKS` is
a second layer that makes the app fail closed if the bind is ever changed to
`0.0.0.0` or a firewall rule is mistakenly loosened. It accepts a comma-separated
list of CIDRs; unset means no application-level check.

`100.64.0.0/10` is the range Tailscale assigns to devices.

> Never bind to `0.0.0.0` on a public VPS without a firewall in front of it.

---

## 4. Keep it running (Linux)

`/etc/systemd/system/zacon.service`:

```ini
[Unit]
Description=Zacon
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/opt/zacon/backend
Environment="ZACON_ALLOWED_NETWORKS=100.64.0.0/10,127.0.0.0/8"
ExecStart=/opt/zacon/backend/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now zacon
```

This binds `0.0.0.0` for simplicity at boot (the tailnet address may not exist
yet when the service starts), so the **firewall must do the work**:

```bash
sudo ufw default deny incoming
sudo ufw allow in on tailscale0
sudo ufw allow 22/tcp          # keep SSH, or restrict it to the tailnet too
sudo ufw enable
```

With `ufw` allowing traffic only on `tailscale0`, plus the allowlist above, port
8000 is unreachable from the public internet even though the process binds to
all interfaces.

### Windows host

Run the same `uvicorn` command and use **NSSM** or Task Scheduler ("run whether
user is logged on or not") to start it at boot. Set the firewall to allow port
8000 only on the Tailscale interface.

---

## 5. Back up the data

Two things are stateful and worth backing up:

- `backend/data/description_lookup.json` — the product→code mappings the app has
  learned. Losing it means re-answering every product once.
- The Excel workbooks themselves, wherever operators keep them.

---

## Current security posture

**What protects the system:** only enrolled Tailscale devices can route to it,
there is no public listener, and the app rejects requests from outside the
allowed networks.

**What does not exist yet:** there are **no user accounts**. Anyone who can reach
the app can open and overwrite workbooks, and actions are not attributable to a
person. With a small number of trusted laptops behind a tailnet that is a
reasonable trade-off; it stops being reasonable as the user count grows or if
actions ever need an audit trail. Add authentication before widening access.

**HTTPS:** traffic inside a tailnet is already encrypted end to end, so plain
HTTP over the tailnet is acceptable here. If you later expose it any other way,
put TLS in front of it.

## Claude assistant

The Assistant tab answers questions about the sales history, and can run a
panel of four specialists who read the book from different angles before one
pass weighs them into a buying recommendation. It uses Anthropic's Claude, on
Claude Haiku 4.5 by default.

The model does no arithmetic on money. Every total, ranking and trend is
computed by the same code behind Insights and handed to Claude as fact; its job
is reading the question and explaining the answer. It is read-only and cannot
change the book.

| Variable | What it is |
| --- | --- |
| `ANTHROPIC_API_KEY` | **Secret.** The Anthropic API key the usage is billed to |
| `ZACON_ASSISTANT_MODEL` | Optional. Defaults to `claude-haiku-4-5`. `claude-sonnet-5` gives stronger answers at a higher price |

Without `ANTHROPIC_API_KEY` the tab says it is not set up, and everything else
works exactly as before.

`ANTHROPIC_API_KEY` is a secret and is treated as one:

- Set it in Vercel under Settings, Environment Variables, then redeploy. For
  local development put it in `backend/.env`, which git ignores.
- Never paste it into a chat, an issue or a commit message. A key that reaches
  a transcript or the git history has to be rotated in the Anthropic Console.
- It is used only in server-to-server calls. `/api/health` is open, so it
  reports `assistant: true|false` and never the key. A test pins that, and a
  second fails if anything shaped like an Anthropic key is ever committed.
- Usage is billed to the account that owns the key. A question is one request;
  a buying recommendation is five. Setting a monthly spend limit in the
  Anthropic Console means a busy month cannot surprise anyone.

The model can be changed without touching code. The request is shaped for
whichever family is named: Haiku takes a fixed thinking budget where the newer
models take adaptive thinking, and each rejects the other's form outright.
