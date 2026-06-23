# Deployment: SEA_Code → SEA_App (Home Assistant App)

Home Assistant now calls "add-ons" **apps**. Publishing uses **prebuilt
multi-arch images** (the recommended way) via the current Home Assistant builder
composite actions.
Docs: https://developers.home-assistant.io/docs/apps/publishing

Two repositories:

- **SEA_Code** (this repo): source code + `Dockerfile` + `config.yaml` +
  GitHub Action. On a build it builds one image per architecture, publishes a
  **generic multi-arch manifest** to GHCR, and writes the app metadata to the
  install repo.
- **SEA_App** (public): the HA app repository users add in HA. Contains
  `repository.yaml` + a `smart_energy_agent/` folder with `config.yaml`
  (referencing the generic GHCR image), README, icons. Populated by the Action.

```
Build in SEA_Code ──> GitHub Action
   ├─ build-image (aarch64, amd64) ─> ghcr.io/dneprojects/{arch}-smart_energy_agent
   ├─ publish-multi-arch-manifest  ─> ghcr.io/dneprojects/smart_energy_agent  (← image: in config.yaml)
   └─ publish-metadata             ─> SEA_App/smart_energy_agent/
HA user ── adds SEA_App URL ──> installs ──> HA pulls the manifest image
```

## One-time setup

### 1. Create the repos
- `dneprojects/SEA_Code` — push this folder into it.
- `dneprojects/SEA_App` — **public**, may start empty.

```bash
git init -b main
git add -A
git commit -m "Smart Energy Agent – initial"
git remote add origin https://github.com/dneprojects/SEA_Code.git
git push -u origin main
```

### 2. Auth for the install repo (GitHub App — auto-rotating, no expiry to manage)
The Action pushes to a **different** repo (SEA_App); the built-in `GITHUB_TOKEN`
is not enough. Instead of a personal token that expires, use a **GitHub App** —
the workflow mints a short-lived installation token per run
(`actions/create-github-app-token`), so there is no long-lived secret to rotate.

One-time setup:
1. GitHub → Settings → Developer settings → **GitHub Apps** → **New GitHub App**.
   - Name e.g. `sea-publish-bot`; Homepage URL anything; **uncheck** "Webhook → Active".
   - Repository permissions: **Contents: Read and write** (nothing else needed).
   - "Where can this app be installed?": Only this account. → **Create**.
2. On the App page: note the **App ID**; under "Private keys" → **Generate a private key**
   (downloads a `.pem`).
3. **Install** the App (App page → Install App → your account) and grant it access
   to **only `dneprojects/SEA_App`**.
4. In **SEA_Code** → Settings → Secrets and variables → Actions, add two secrets:
   - `APP_ID` = the App ID (number)
   - `APP_PRIVATE_KEY` = the full contents of the `.pem` file

The workflow's `publish-metadata` job generates the token from these and uses it
to push the metadata; the token expires ~1 h after the run on its own.

Building/pushing images to GHCR uses the built-in `GITHUB_TOKEN` (jobs have
`packages: write` and `id-token: write`).

### 3. Icons (optional, recommended)
Place `icon.png` (~256×256, square) and `logo.png` in the `web/` folder. The
workflow copies them into the published app folder, and `web/logo.png` is also
served as the in-app header logo (`static/logo.png`).

## Release Manager: Public (main) & Beta (beta) — like SmartHub-Addon

The "Smart Energy Agent Release Manager" workflow drives both channels, analogous
to the SmartHub Release Manager:

- **Auto-beta:** every commit on `main` builds the beta.
- **Manual:** "Run workflow" with target **Beta** or **Public**.
- **Release:** a published release builds Public.

| Channel | SEA_App branch | Slug                      | Name                      | Image                                         |
|---------|----------------|---------------------------|---------------------------|-----------------------------------------------|
| Public  | `main`         | `smart_energy_agent`      | Smart Energy Agent        | `ghcr.io/dneprojects/smart_energy_agent`      |
| Beta    | `beta`         | `smart_energy_agent_beta` | Smart Energy Agent (Beta) | `ghcr.io/dneprojects/smart_energy_agent_beta` |

Separate branch, slug and image per channel, so both can be installed in
parallel. The name/slug HA shows comes from `name:`/`slug:` in the `config.yaml`
on the respective branch (like SmartHub's `smarthub_beta`). The beta version
carries `…-beta.<run>` so HA recognizes each new beta as an update.

## Build a release
1. Bump `version` in `config.yaml`, commit/push.
2. In **SEA_Code** create a **release** with a tag (e.g. `v0.2.0`) — or run the
   Action manually via "Run workflow" → Public.
3. The Action builds `aarch64` + `amd64`, publishes the manifest, and commits the
   metadata to SEA_App.

### Make the GHCR packages public (one-time)
GitHub → profile → **Packages**: the generic package `smart_energy_agent`
(and the arch packages `aarch64-…`, `amd64-…`) → Package settings →
**Change visibility → Public**. Otherwise HA cannot pull the image without login.

## Install in Home Assistant
Settings → Add-ons/Apps → Store → ⋮ → **Repositories** →
`https://github.com/dneprojects/SEA_App` → install "Smart Energy Agent".

## Versioning
The source of truth is `version` in `config.yaml`. Bump version → release/tag (or
manual run) → Action → HA offers the update.

## Same for SmartHub-Addon
`SmartHub-Addon` is already an app repository but lacks a current build workflow.
Reuse `.github/workflows/build.yaml` and adapt it: set the slug/image to
`smart_hub`. Since source and app live in the same repo there, the
`publish-metadata` job can be dropped (or commit into the same repo).

## Notes
- Builder actions are pinned to `@2026.03.2` (per the HA docs). Update the tag
  for newer releases.
- Label `io.hass.type=app` (new; formerly `addon`).
- The base image and labels live in the `Dockerfile` (`ARG BUILD_FROM=...` +
  `LABEL`); `build.yaml` is no longer used.
- Dev files (`Dockerfile.standalone`, `docker-compose.yml`, `examples/`) are
  irrelevant to the app image — it is built from `Dockerfile`, `config.yaml` and
  the source code.
