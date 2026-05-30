# Deployment: SAE_Code → SAE_App (Home Assistant App)

Home Assistant nennt „Add-ons" inzwischen **Apps**. Veröffentlicht wird mit
**vorgebauten Multi-Arch-Images** (empfohlener Weg) über die aktuellen
Home-Assistant-Builder-Composite-Actions.
Doku: https://developers.home-assistant.io/docs/apps/publishing

Zwei Repositories:

- **SAE_Code** (dieses Repo): Quellcode + `Dockerfile` + `build.yaml` +
  `config.yaml` + GitHub-Action. Baut bei einem Release je Architektur ein Image,
  veröffentlicht ein **generisches Multi-Arch-Manifest** nach GHCR und schreibt
  die App-Metadaten ins Install-Repo.
- **SAE_App** (öffentlich): das HA-App-Repository, das Nutzer in HA hinzufügen.
  `repository.yaml` + Ordner `smart_energy_agent/` mit `config.yaml`
  (referenziert das generische GHCR-Image), README, Icons. Wird von der Action befüllt.

```
Release in SAE_Code ──> GitHub Action
   ├─ build-image (aarch64, amd64) ─> ghcr.io/dneprojects/{arch}-smart_energy_agent
   ├─ publish-multi-arch-manifest  ─> ghcr.io/dneprojects/smart_energy_agent  (← image: in config.yaml)
   └─ publish-metadata             ─> SAE_App/smart_energy_agent/
HA-Nutzer ── fügt SAE_App-URL hinzu ──> installiert ──> HA zieht das Manifest-Image
```

## Einmalige Einrichtung

### 1. Repos anlegen
- `dneprojects/SAE_Code` — diesen Ordner hineinpushen.
- `dneprojects/SAE_App` — **öffentlich**, darf zunächst leer sein.

```bash
git init -b main
git add -A
git commit -m "Smart Energy Agent – initial"
git remote add origin https://github.com/dneprojects/SAE_Code.git
git push -u origin main
```

### 2. Token für das Install-Repo
Die Action pusht in ein **anderes** Repo (SAE_App); das automatische
`GITHUB_TOKEN` reicht dafür nicht. Fine-grained Token erstellen:
- GitHub → Settings → Developer settings → Fine-grained tokens
- Repository access: nur `dneprojects/SAE_App`; Permission **Contents: Read and write**
- In **SAE_Code** → Settings → Secrets and variables → Actions → Secret
  `APP_REPO_TOKEN` = das Token.

Das Bauen/Pushen der Images nach GHCR nutzt das eingebaute `GITHUB_TOKEN`
(Jobs haben `packages: write` und `id-token: write`).

### 3. Icons (optional, empfohlen)
`icon.png` (~256×256) und `logo.png` im Wurzelverzeichnis ablegen (z. B. das
Habitron-Logo) — werden mit veröffentlicht.

## Release Manager: Public (main) & Beta (beta) — wie SmartHub-Addon

Der Workflow „Smart Energy Agent Release Manager" steuert beide Kanäle, analog
zum SmartHub Release Manager:

- **Auto-Beta:** jeder Commit auf `main` baut die Beta.
- **Manuell:** „Run workflow" mit Ziel **Beta** oder **Public**.
- **Release:** ein veröffentlichtes Release baut Public.

| Kanal  | Branch von SAE_App | Slug                     | Name                       | Image                                   |
|--------|--------------------|--------------------------|----------------------------|-----------------------------------------|
| Public | `main`             | `smart_energy_agent`     | Smart Energy Agent         | `ghcr.io/dneprojects/smart_energy_agent`      |
| Beta   | `beta`             | `smart_energy_agent_beta`| Smart Energy Agent (Beta)  | `ghcr.io/dneprojects/smart_energy_agent_beta` |

Eigener Branch, eigener Slug und eigenes Image je Kanal — beide lassen sich also
parallel installieren. Der in HA gezeigte Name/Slug kommt aus `name:`/`slug:`
der `config.yaml` auf dem jeweiligen Branch (wie SmartHubs `smarthub_beta`).
Die Beta-Version trägt `…-beta.<run>`, damit HA jede neue Beta als Update erkennt.

## Release bauen
1. `version` in `config.yaml` erhöhen, committen/pushen.
2. In **SAE_Code** ein **Release** mit Tag (z. B. `v0.2.0`) anlegen — oder die
   Action manuell über „Run workflow" starten.
3. Die Action baut `aarch64` + `amd64`, veröffentlicht das Manifest und
   committet die Metadaten nach SAE_App.

### GHCR-Pakete öffentlich schalten (einmalig)
GitHub → Profil → **Packages**: das generische Paket `smart_energy_agent`
(und die Arch-Pakete `aarch64-…`, `amd64-…`) → Package settings →
**Change visibility → Public**. Sonst kann HA das Image nicht ohne Login ziehen.

## Installation in Home Assistant
Einstellungen → Add-ons/Apps → Store → ⋮ → **Repositories** →
`https://github.com/dneprojects/SAE_App` → „Smart Energy Agent" installieren.

## Versionierung
Quelle der Wahrheit ist `version` in `config.yaml`. Version erhöhen → Release/Tag
→ Action → HA bietet das Update an.

## Dasselbe für SmartHub-Addon
`SmartHub-Addon` ist bereits ein App-Repository, hat aber keinen aktuellen
Build-Workflow. Übernimm `.github/workflows/build.yaml` und passe an:
`ADDON_SLUG: smart_hub`, in dessen `config.yaml`
`image: ghcr.io/dneprojects/smart_hub` (generisch) setzen und `build.yaml` um
`amd64` ergänzen, falls gewünscht. Da dort Quelle und App im selben Repo liegen,
kann der `publish-metadata`-Job entfallen (oder ins selbe Repo committen).

## Hinweise
- Builder-Actions sind auf `@2026.03.2` gepinnt (Stand der HA-Doku). Bei neueren
  Releases den Tag aktualisieren.
- Label `io.hass.type=app` (neu; früher `addon`).
- Dev-Dateien (`Dockerfile.standalone`, `docker-compose.yml`, `examples/`) sind
  fürs App-Image irrelevant — gebaut wird mit `Dockerfile`, `build.yaml`,
  `config.yaml` und dem Quellcode.
