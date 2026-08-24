# Deploying to the Oracle Cloud Always Free VM

Real, verified steps for the `VM.Standard.A1.Flex` (Ampere, Always
Free-eligible, works in any availability domain -- more reliable than
`VM.Standard.E2.1.Micro`, which is capacity-constrained to a specific
AD that varies) Oracle Linux 9 instance this project actually runs on.

## 1. VM + networking

- Create the instance (Oracle Linux 9 image -- **not** the default
  image's "BM Confidential computing" security option, which silently
  filters out every non-bare-metal shape from the picker; switch the
  image to plain Oracle Linux 9 or Ubuntu first).
- VCN: use the **"Create a VCN with internet connectivity"** wizard
  (Networking → Overview → "Start VCN wizard"), not the bare "Create
  VCN" form -- the bare form doesn't provision a working public
  subnet/internet gateway, and the instance-creation page's own inline
  "create a VCN" shortcut has a real bug where the public-IP toggle
  stays permanently disabled.
- Security List: add ingress rules for TCP 80 and 443 from `0.0.0.0/0`
  (22 is open by default).
- OS firewall (`firewalld`, on by default on Oracle Linux -- a second,
  separate layer from the Security List above):
  ```
  sudo firewall-cmd --permanent --add-service=http
  sudo firewall-cmd --permanent --add-service=https
  sudo firewall-cmd --reload
  ```

## 2. SELinux (real, recurring gotcha)

Oracle Linux 9 runs SELinux in **Enforcing** mode. Any file staged via
`scp`/`mv` from `/tmp` keeps `/tmp`'s `user_tmp_t` label, which silently
breaks things with confusing, unrelated-looking errors -- systemd
reports **"Failed to load environment files: Permission denied"** for a
mislabeled `.env`, and **`status=203/EXEC`** for a mislabeled binary,
neither of which mentions SELinux at all. Fix: `restorecon -R` on
anything moved from `/tmp`, most importantly the whole app directory
after cloning/installing and any systemd unit file after copying it in:
```
sudo restorecon -R /opt/career-pilot
sudo restorecon -v /etc/systemd/system/<unit>.service
```

## 3. Python

Oracle Linux 9's default `python3` is 3.9 -- too old to reliably match
`requirements-lock.txt` (pinned from a 3.13 dev environment). Install
3.11 directly from Oracle's own `ol9_appstream` repo (no EPEL needed):
```
sudo dnf install -y python3.11 python3.11-pip git
```

## 4. Database connection (real Supabase gotcha)

Supabase's **direct** database hostname (`db.<ref>.supabase.co`) is
**IPv6-only** for many projects -- it has no IPv4 (A) record at all.
A VM without IPv6 routing (the default here -- IPv6 was never enabled
on the VCN) fails with `OperationalError: Network is unreachable`, not
an auth or DNS error. Fix: use Supabase's **connection pooler**
instead, which supports IPv4 (Supabase dashboard → Project Settings →
Database → Connection pooling → copy the pooler connection string,
looks like `postgresql://postgres.<ref>:<password>@aws-<n>-<region>
.pooler.supabase.com:6543/postgres`).

## 5. App setup

```
sudo useradd --system --create-home --home-dir /opt/career-pilot --shell /usr/sbin/nologin career-pilot
sudo -u career-pilot git clone git@github.com:Deshraj-Jogiya/Job-Search-CRM-Automation.git /opt/career-pilot
sudo -u career-pilot python3.11 -m venv /opt/career-pilot/venv
sudo -u career-pilot /opt/career-pilot/venv/bin/pip install -r /opt/career-pilot/requirements-lock.txt
# scp your real .env over (never through git), then:
sudo chown career-pilot:career-pilot /opt/career-pilot/.env && sudo chmod 600 /opt/career-pilot/.env
sudo restorecon -R /opt/career-pilot
sudo cp /opt/career-pilot/deploy/career-pilot.service /etc/systemd/system/
sudo restorecon -v /etc/systemd/system/career-pilot.service
sudo systemctl daemon-reload && sudo systemctl enable --now career-pilot
```

**Before starting the service**, set `DASHBOARD_PASSWORD` and
`APP_BASE_URL` in `.env` -- without `DASHBOARD_PASSWORD` the dashboard
is wide open to anyone reaching the server the moment it's exposed
publicly.

## 6. Interactive autofill needs a real, visible browser

Autofill deliberately opens a real, visible (`headless=False`) browser
so a human reviews and clicks Submit themselves (see
`autofill_service.py`'s docstring) -- there is no headless mode for
this by design. A remote headless VM has no display for that browser to
open on, so this deployment runs a virtual one instead:

- **Xvfb** (`xvfb.service`) -- a virtual X display (`:99`).
- **Openbox** (`openbox.service`) -- a minimal window manager, so the
  browser window isn't undecorated/awkward to interact with.
- **x11vnc** (`x11vnc.service`) -- shares that display over VNC,
  bound to `127.0.0.1` only (never exposed directly).
- **noVNC + websockify** (`novnc.service`) -- a web-based VNC client,
  also bound to `127.0.0.1` only.
- **Caddy** proxies `/vnc/*` to noVNC with its own Basic Auth layer on
  top of the VNC password -- this view can show real personal/
  application data, so it gets defense in depth, not just one password.
- `career-pilot.service` gets `Environment=DISPLAY=:99` so the autofill
  browser opens there. Note `PrivateTmp` is deliberately **not** set on
  this unit (unlike a typical hardened service) -- it would isolate the
  service from the real `/tmp/.X11-unix` socket Xvfb listens on.

Install:
```
sudo dnf install -y xorg-x11-server-Xvfb x11vnc openbox
sudo dnf install -y atk at-spi2-atk at-spi2-core cups-libs libxkbcommon alsa-lib libXcomposite libXdamage libXfixes libXrandr mesa-libgbm libdrm pango cairo libXext nss nspr libxshmfence liberation-fonts
sudo -u career-pilot /opt/career-pilot/venv/bin/pip install websockify
sudo git clone --depth 1 https://github.com/novnc/noVNC.git /opt/novnc
sudo x11vnc -storepasswd <a-real-password> /etc/x11vnc/vncpasswd.txt
sudo chown career-pilot:career-pilot /etc/x11vnc/vncpasswd.txt && sudo chmod 600 /etc/x11vnc/vncpasswd.txt
# copy xvfb.service, openbox.service, x11vnc.service, novnc.service to /etc/systemd/system/, restorecon, then:
sudo systemctl daemon-reload
sudo systemctl enable --now xvfb openbox x11vnc novnc
```

Playwright's own dependency installer (`playwright install-deps`) only
supports Ubuntu/Debian and hard-fails on Oracle Linux even once every
real dependency is present -- install the system packages above
manually, then skip its (already-satisfied) validation check:
```
sudo -u career-pilot env PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1 /opt/career-pilot/venv/bin/playwright install chromium
```

Caddyfile (`/etc/caddy/Caddyfile`, adjust the domain and generate a
real password hash with `caddy hash-password --plaintext <password>`):
```
your-domain-or-sslip-io-address {
    handle_path /vnc/* {
        basicauth {
            <username> <bcrypt-hash>
        }
        reverse_proxy 127.0.0.1:6080
    }
    reverse_proxy 127.0.0.1:8000
}
```

## 7. HTTPS without an owned domain

[sslip.io](https://sslip.io) resolves `<ip-with-dashes>.sslip.io` to
that IP automatically, with zero DNS setup -- e.g. `129-146-36-193
.sslip.io` for `129.146.36.193`. That's a real, publicly resolvable
hostname, so Caddy can get a real Let's Encrypt certificate for it with
no extra configuration. Swap in a real owned domain later just by
changing the Caddyfile's site address and pointing its DNS at the VM.
