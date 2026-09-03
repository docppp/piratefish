# ARR Stack Appliance

A near **zero-touch installer** that stands up a complete *Arr media stack in
Docker and wires every integration for you. You provide one data path; the
installer creates the containers, generates all API links, sets up the download
client, indexers, subtitles, media server and a dashboard — then prints ready-to-use
LAN URLs.

## What you get

| Service | Purpose | Default port |
|---|---|---|
| **Prowlarr** | Indexer/tracker manager (feeds Sonarr & Radarr) | 9696 |
| **Sonarr** | TV automation | 8989 |
| **Radarr** | Movie automation | 7878 |
| **Bazarr** | Subtitles for Sonarr/Radarr | 6767 |
| **qBittorrent** | Download client | 8080 |
| **Jellyseerr** | Request management UI for Sonarr/Radarr | 5055 |
| **Jellyfin** | Media server (watch from any device) | 8096 |
| **Homepage** | Single dashboard linking everything | 3000 |

No VPN, FlareSolverr, Recyclarr or Unpackerr are installed (kept intentionally lean;
the architecture leaves room to add them later).

## Requirements

- **Linux**: any modern distro. The installer auto-installs Docker Engine + the
  Compose plugin if missing.
- **Windows** (native): the installer is a thin bootstrapper. It ensures **WSL2**
  and creates its **own dedicated distro** named **`PirateFish-Ubuntu`** (so it
  never touches any other WSL distros you use), then re-runs *this same installer
  inside WSL2*, where Docker Engine is installed and run exactly like on native
  Linux. It **requires Administrator rights** (it auto-elevates via a UAC prompt)
  — needed to enable WSL2 and to open the Windows firewall + port-forwarding so
  **other devices on your LAN** can reach the services. (Your own Windows browser
  reaches them at `http://localhost:<port>`
  even without that.)
- Python **3.8+** (standard library only — no `pip install` required).

## Quick start

```bash
python install.py
```

When a graphical display is available, this opens a **desktop GUI wizard**
(pywebview). On headless servers, or with `--no-gui`, it runs the same flow in
the **console**. Both share one engine.

> **On Windows**, just run `python install.py` — it **auto-elevates** (UAC prompt)
> since Administrator rights are needed to enable WSL2 and open LAN firewall/port
> forwarding. It bootstraps WSL2 and then continues **inside WSL2**. The graphical
> wizard is shown through **WSLg** (built into Windows 11 and up-to-date Windows
> 10); if WSLg isn't available it falls back to the console automatically.

You'll be asked for:

1. **Data path** — one folder that holds everything (e.g. `/mnt/media/ArrStack`
   on Linux, `D:\Media\ArrStack` on Windows).
2. **Timezone** (auto-detected).
3. **Web UI username / password**.

Then it runs fully automatically: dependency checks → filesystem/hardlink test →
container start → integration bootstrap → firewall rules → verification → report.

### Basic Arr setup (after install)
When finished, the GUI offers an optional **"basic Arr setup"**:

- **Prowlarr trackers** — search Prowlarr's live catalog and add trackers. For
  **private trackers that need a cookie**, an embedded login window opens; you log
  in normally (Cloudflare/CAPTCHA/2FA all work) and the session cookie is captured
  and saved automatically. Each indexer is tested before saving and auto-syncs to
  Sonarr/Radarr.
- **Bazarr subtitles** — pick your subtitle **language** and providers. **English
  is always kept as a fallback** when your language isn't available. A default
  language profile is created and assigned to Series & Movies.
- **Quality** — choose target resolution, allowed release source types (e.g.
  BluRay / WEB-DL / WEBRip / HDTV), and max bitrate. The installer applies a
  single Sonarr/Radarr profile named `piratefish_default` and removes the
  other quality profiles.
- **Extras** — final links & credentials summary.

### GUI vs console
```bash
python install.py --gui       # force the graphical wizard
python install.py --no-gui    # force the console installer
```
The GUI needs a native webview: **Linux and Windows (inside WSL2 via WSLg)** use
WebKit2GTK (the installer installs `python3-gi`/`gir1.2-webkit2` and `pywebview`
into a local `.venv` when needed). If any of that is unavailable, it falls back to
the console automatically.

### Other commands

```bash
python install.py doctor /mnt/media/ArrStack   # test a path's filesystem only
python install.py verify                        # re-check a running stack
python install.py up                            # start the stack (idempotent) + open dashboard
python install.py down                          # stop the stack
```

`up` and `down` are safe to run repeatedly: `up` detects an already-running
stack, reconciles it, re-opens the dashboard and reports "already running"
instead of erroring or spawning duplicates; `down` on an already-stopped stack
is a friendly no-op. Neither ever deletes your data or config.

## Data layout (single path `X`)

```
X/
├── Torr/            # downloads live here (and stay while seeding)
│   ├── incomplete/
│   ├── tv/          # qBittorrent tv category  — also your manual TV drops
│   └── movies/      # qBittorrent movie category — also your manual movie drops
├── Media/
│   ├── Series/      # Sonarr library  (hardlink target)
│   └── Movies/      # Radarr library  (hardlink target)
└── Arr/             # app config/persistence (prowlarr, sonarr, ... , jellyseerr, homepage)
```

Everything is bind-mounted into the containers as a **single `/data` mount**, so
`Torr` and `Media` share one filesystem. That's what makes **hardlinks** work:
imports are instant and cost no extra disk, and the original stays put for seeding.

## Two ways to add media

1. **Automated** — add a show/movie in Sonarr/Radarr. They search via Prowlarr,
   download through qBittorrent into `Torr/`, then hardlink+rename into `Media/`.
2. **Manual drop** — copy your own files straight into `X/Torr` (e.g. `Torr/tv`
   or `Torr/movies`), then use **Manual Import** in Sonarr/Radarr. Because it's the
   same `/data` mount, the import is an instant hardlink. No extra folder needed.

## Seeding defaults

- Stop seeding at **ratio 2.0** *or* **7 days**, whichever comes first, then
  **pause** (never auto-delete) so your data stays available.
- Sonarr/Radarr keep completed downloads in `Torr/` and import by hardlink, so a
  file can seed and live in your library at the same time.

Change these in qBittorrent → *Options → BitTorrent* anytime.

## Guided tracker setup

Prowlarr is the one place you add indexers. The installer runs a wizard driven by
Prowlarr's live indexer catalog:

- Search a tracker by name and pick it.
- **Public** trackers are added directly.
- **Private** trackers prompt for exactly what that site needs — username/password,
  API key, passkey, RSS key, or a login cookie — entered masked and stored only
  inside Prowlarr.
- Each indexer is **tested before saving**; on failure you can retry or skip.

You can skip and add trackers later in the Prowlarr UI. Added indexers sync to
Sonarr and Radarr automatically.

## LAN access

Every WebUI is published on the host, so any device on your network can reach
`http://<host-ip>:<port>`.

- **Linux**: the installer opens `ufw`/`firewalld` ports if either is active.
- **Windows**: because the services run inside WSL2 (behind NAT), reaching them
  from other devices needs Windows Firewall rules **plus** `netsh` port-forwarding
  to the WSL2 VM — and the WSL2 IP changes on every reboot. The installer writes a
  self-contained **`piratefish_startup.bat`** into the install folder and puts a
  **PirateFish** shortcut (with icon) on your Desktop pointing at it. Clicking it
  starts the stack inside WSL2 **and** re-applies the firewall + port-forwarding
  for the current WSL2 IP (it self-elevates with a single UAC confirm, needed for
  `netsh`). The `.bat` talks only to WSL2 (Docker + the compose bundle living at
  `/opt/piratefish` inside the distro), so it keeps working even if you delete the
  Python installer files. (On the Windows PC itself, `http://localhost:<port>`
  also works.)

## Windows + WSL2 notes (Docker in WSL2, media on a Windows drive)

Windows-hosted media is a **supported, deliberate** setup. On Windows the
installer opens a **native folder picker** so you choose the media/data folder on
**any Windows drive** (e.g. `D:\Media`, including a separate/secondary disk). That
folder is mounted into WSL2 at `/mnt/<drive>/...` and bind-mounted into the
containers, so your library stays on the Windows disk you picked. The installer's
`doctor` step then tests the real behaviour of that path and reports caveats:

- **Hardlinks**: verified on the volume (imports fail loudly if unavailable, e.g.
  if `Torr` and `Media` end up on different drives — keep `X` on one disk).
- **chmod/chown**: no-ops on Windows-hosted filesystems; harmless for these images.
- **inotify/file-watch**: unreliable on `/mnt/<drive>`; Sonarr/Radarr fall back to
  periodic library scans (a manual drop is noticed on the next scan).
- **Performance**: slower than native Linux storage.

If your BIOS has virtualization disabled, WSL2 can't be enabled automatically —
the installer tells you to turn on Intel VT-x / AMD-V and re-run.

## Image versions

All images are **pinned** (no `:latest`) in `installer/constants.py` and surfaced
as `*_IMAGE` variables in the generated `.env`, so you can override or upgrade a
single service without editing code.

## Files

- `install.py` — entrypoint launcher.
- `installer/` — the OS-agnostic installer package.
- `.env`, `docker-compose.yml` — generated at install time (git-ignored; the
  `.env` holds secrets, written `0600`).
- `install-report.txt` — written into your data path with URLs, credentials and
  API keys.
- `piratefish.png` — used as the Homepage dashboard background (copied to
  `X/Arr/homepage/images/` during bootstrap).
- `PirateFish.sh` (Linux) launcher lives next to the generated runtime
  `docker-compose.yml` + `.env` bundle in your user profile. On **Windows** the
  installer instead writes a self-contained **`piratefish_startup.bat`** into the
  install folder and a **PirateFish** desktop shortcut (with icon) pointing at it;
  the `.bat` drives WSL2 directly (Docker + the `/opt/piratefish` compose bundle)
  and re-applies LAN forwarding, so it works even if the Python installer files
  are removed. If the desktop shortcut is deleted, rerunning the installer
  recreates it. Double-clicking it repeatedly is safe.
- A small host **control panel** (`http://<host-ip>:8787`) the installer also
  starts. Homepage includes a **Shut down stack** button that calls this host endpoint
  directly, so you can shut down the whole stack (including Homepage
  itself) from the dashboard. The control panel URL remains reachable from any
  device as a manual fallback.

## Turning the stack on/off

After install, the stack starts automatically. To control it later:

- **Easiest:** double-click **PirateFish** on your desktop to bring the
  stack up; use the dashboard **Shut down stack** button to turn it off.
- **Terminal (from this folder):**
  ```bash
  python install.py up        # start everything (idempotent) + open dashboard
  python install.py down      # stop everything
  ```
  On Windows these bootstrap WSL2 and run the stack inside it. To drive Docker
  directly, open the distro (`wsl -d Ubuntu`) and use `docker compose up -d` /
  `docker compose down` from `/opt/piratefish`.

> Tip: don't move or rename your data folder while the stack is running — the
> containers are bind-mounted to it. If you do, just run the launcher / `docker
> compose up -d` again and the installer's `--force-recreate` re-binds correctly.
