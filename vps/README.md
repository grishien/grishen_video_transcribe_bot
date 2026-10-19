# VPS inbox

Plain HTTP receiver. Android uploads an mp4 with `PUT /upload`. The PC worker lists, downloads, and deletes files. There is no domain, no HTTPS, and no reverse proxy. Do not install or configure Caddy or nginx for this service.

The only authentication is a long bearer token. Anyone who can see the traffic can see the token. That is the setup this phase uses.

**Do not change Amnezia or frp.** Do not edit their configs, do not change their ports, and do not restart those services while deploying this receiver. This unit listens on its own port only.

## Generate a token

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

On a server where `python` is not on `PATH`, use `python3` with the same argument. The process refuses to start if `TOKEN` is shorter than 32 characters. Keep the value in `/etc/transcribe-inbox.env` only. Do not commit it and do not paste it into the unit file.

## Pick a free port

```bash
sudo ss -tlnp
```

The `Local Address` column lists ports that are already taken. Choose a TCP port that does not appear. Do not reuse a port used by SSH, Amnezia, frp, or anything else.

## Deploy

Copy this `vps` directory to the server (the clone you already have, `scp`, or otherwise). On the server, from inside that directory, deploy either with `install.sh` or by hand. Both install the same unit. Neither edits Amnezia or frp, and neither restarts those services.

### install.sh

```bash
sudo bash install.sh --token 'paste-the-generated-token' --port PASTE_PORT
```

Leave out `--token` or `--port` and the script asks for them. Optional `--max-size-mb` defaults to 500. The script creates the user, the venv, `/etc/transcribe-inbox.env` (root, mode 600), and the systemd unit, then starts only `transcribe-inbox`. It refuses a port that is already taken by another process. If `ufw` or `firewalld` is already active, it opens this TCP port and leaves the other rules as they are. It does not enable a firewall that is off. If the provider panel has a firewall, open the same port there yourself. Running the script again updates the app files and restarts only this service.

### By hand

Python 3.10 or newer is required (`python3 --version`). These steps assume a Debian or Ubuntu VPS. On another distribution, install `python3` and `python3-venv` with that distribution's packages. Do not upgrade unrelated packages.

```bash
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin transcribe-inbox

sudo mkdir -p /opt/transcribe-inbox /var/lib/transcribe-inbox
sudo cp main.py requirements.txt /opt/transcribe-inbox/
sudo python3 -m venv /opt/transcribe-inbox/.venv
sudo /opt/transcribe-inbox/.venv/bin/pip install -r /opt/transcribe-inbox/requirements.txt

sudo chown -R root:root /opt/transcribe-inbox
sudo chmod -R u=rwX,go=rX /opt/transcribe-inbox
sudo chown transcribe-inbox:transcribe-inbox /var/lib/transcribe-inbox
sudo chmod 700 /var/lib/transcribe-inbox
```

`u=rwX,go=rX` keeps the code and the venv readable and executable by the service user, and not writable by it. If `useradd` reports that the user already exists, skip that one command.

`/opt/transcribe-inbox` is root-owned application code. `/var/lib/transcribe-inbox` is the only directory the service user can write. Create that inbox directory before starting the unit: the unit mounts it read-write, and the path has to exist.

If `useradd` says the shell path does not exist, substitute the `nologin` path from `command -v nologin`.

Create the env file as root, mode `600`. Replace the token and the port. Leave `INBOX_DIR` as written unless you also change `ReadWritePaths` in the unit to the same directory.

```bash
sudo install -m 600 -o root -g root /dev/null /etc/transcribe-inbox.env
sudo tee /etc/transcribe-inbox.env >/dev/null <<'EOF'
TOKEN=paste-the-generated-token
INBOX_DIR=/var/lib/transcribe-inbox
MAX_SIZE_MB=500
PORT=paste-the-free-port
EOF
sudo chown root:root /etc/transcribe-inbox.env
sudo chmod 600 /etc/transcribe-inbox.env
```

`MAX_SIZE_MB` defaults to 500 when unset. The limit is `MAX_SIZE_MB * 1024 * 1024` bytes. One `KEY=VALUE` per line, no quotes.

Install the unit and start it. This does not restart other services.

```bash
sudo cp deploy/transcribe-inbox.service /etc/systemd/system/transcribe-inbox.service
sudo systemctl daemon-reload
sudo systemctl enable --now transcribe-inbox.service
systemctl status transcribe-inbox.service
sudo ss -tlnp | grep ':<your-port>'
```

Logs:

```bash
journalctl -u transcribe-inbox.service -f
```

The process listens on `0.0.0.0:$PORT`. A leftover `*.mp4.part` file is an upload that was interrupted by a crash before cleanup. It is not a ready video. `GET /files` does not list `.part` files. You can delete stale `.part` files by hand.

## Open the port

Open only the new TCP port. Do not flush firewall rules. Do not change rules for SSH, Amnezia, or frp. Do not restart Amnezia or frp.

If the hosting provider has a firewall or security group in its panel, allow inbound TCP on this port there as well. Leave every existing panel rule as it is.

On the VPS, use whichever firewall is already active. Do not enable a second one.

`ufw`, only if `sudo ufw status` shows it is active. If it is inactive, do not enable it (that can lock out SSH and interfere with services that are already running):

```bash
sudo ufw allow PORT/tcp comment 'transcribe-inbox'
```

`firewalld`, only if `systemctl is-active firewalld` prints `active`:

```bash
sudo firewall-cmd --permanent --add-port=PORT/tcp
sudo firewall-cmd --reload
```

`--reload` reapplies firewalld's saved rules, including this new port. It does not edit Amnezia or frp, and it does not restart them.

If you manage `nftables` or `iptables` yourself, add one accept rule for this TCP port. Do not flush or replace the ruleset.

## Check with curl

Run these on the VPS first (`127.0.0.1`), then from your PC with `http://<VPS_IP>:<PORT>`. Replace `TOKEN`, `PORT`, and the sample file. `--data-binary` sends the raw bytes; the original file name is ignored.

```bash
TOKEN='paste-the-generated-token'
PORT=paste-the-free-port
BASE="http://127.0.0.1:${PORT}"
```

401 without a token, and 401 with the wrong token:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X PUT --data-binary @sample.mp4 "${BASE}/upload"
curl -sS -o /dev/null -w '%{http_code}\n' -X PUT -H "Authorization: Bearer wrong-token-wrong-token-wrong-token" --data-binary @sample.mp4 "${BASE}/upload"
curl -sS -o /dev/null -w '%{http_code}\n' "${BASE}/files"
```

Each of those prints `401`.

Upload. The response is `201` and `{"id":"..."}`. A file `inbox/<id>.mp4` is on disk. There is no `<id>.mp4.part` next to it.

```bash
curl -sS -D - -X PUT -H "Authorization: Bearer ${TOKEN}" --data-binary @sample.mp4 "${BASE}/upload"
sudo ls -l /var/lib/transcribe-inbox
```

List ready files (no `.part` entries), download, and compare bytes:

```bash
curl -sS -H "Authorization: Bearer ${TOKEN}" "${BASE}/files"
curl -fsS -H "Authorization: Bearer ${TOKEN}" -o /tmp/down.mp4 "${BASE}/files/PASTE_ID"
cmp sample.mp4 /tmp/down.mp4 && echo identical
```

Delete. The first command ends with `204`. The file is gone. A second delete, or a download of the same id, is `404`.

```bash
curl -sS -D - -o /dev/null -X DELETE -H "Authorization: Bearer ${TOKEN}" "${BASE}/files/PASTE_ID"
sudo ls -l /var/lib/transcribe-inbox
```

Too large (over `MAX_SIZE_MB`) is `413` and does not leave a file:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X PUT -H "Authorization: Bearer ${TOKEN}" --data-binary @too-big.mp4 "${BASE}/upload"
```

An id that is not a uuid is `400`, including a path-traversal attempt:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer ${TOKEN}" "${BASE}/files/not-a-uuid"
curl --path-as-is -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer ${TOKEN}" "${BASE}/files/..%2F..%2Fetc%2Fpasswd"
```

Interrupted upload: declare a normal body, then kill curl before it finishes. Afterwards the inbox has no new `.mp4` and no `.mp4.part` from that attempt.

```bash
dd if=/dev/urandom of=/tmp/abort-test.bin bs=1024 count=256 status=none
curl --limit-rate 1000 --max-time 1 -sS -o /dev/null -w '%{http_code}\n' \
  -X PUT -H "Authorization: Bearer ${TOKEN}" \
  --data-binary @/tmp/abort-test.bin \
  "${BASE}/upload" || true
sleep 1
sudo find /var/lib/transcribe-inbox -name '*.mp4' -o -name '*.mp4.part'
```

A missing `Content-Length` is `411`.

After the port is open in the firewall and in the provider panel, repeat the 401 and the upload from your PC against `http://<VPS_IP>:<PORT>`.

Amnezia and frp should still be running on their original ports. Check with `sudo ss -tlnp` and by using them the way you usually do. Nothing in this deploy restarts them.
