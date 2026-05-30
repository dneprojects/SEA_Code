# Smart Energy Agent

Energiebezogene Erkennung, Überwachung und (später) tarif-/PV-optimierte Steuerung
für Home Assistant. Aktueller Stand: Phase 1 – Monitoring & Kuratierung.

Konzept: siehe [`EnergyAgent_Konzept.md`](EnergyAgent_Konzept.md).

## Bedienung (Phase 1)

Die UI zeigt die erkannten Entitäten gruppiert nach Energierolle. Konfig-/
Diagnose-Entitäten (LEDs, Flags, Setpoints, Geräte-Batteriestände) werden
automatisch ausgeblendet. Messgrößen (PV/Netz/Batterie/Leistung/Energie) sind
standardmäßig einbezogen, steuerbare Kandidaten (Schalter/Lampen/Klima) nicht –
diese per Haken aktivieren. Über das Rollen-Dropdown lässt sich eine
Fehlklassifizierung korrigieren. Die Auswahl wird in `overrides.json`
(neben der History-DB, i. d. R. `/data`) gespeichert und überlebt Neustarts.

Umschalter „nur einbezogene / alle Kandidaten" blendet die noch nicht
einbezogenen Vorschläge ein.

## Testumgebung ohne echte PV/Batterie

Wenn das Test-HA keine PV-/Speicher-Anlage kennt, simulieren die
Template-Sensoren in [`examples/ha_sim_sensors.yaml`](examples/ha_sim_sensors.yaml)
einen realistischen Tagesverlauf (PV, Hausverbrauch, Batterie-SoC/-Leistung,
Netz). Einbindung: Datei-Kopf in `examples/ha_sim_sensors.yaml`.

## Zwei Betriebsarten

### A) Standalone-Container (für HA Container / Entwicklung)

Empfohlen, wenn HA selbst als Container läuft (kein Supervisor, keine Add-ons).
Smart Energy Agent verbindet sich per Websocket + Long-Lived-Token.

```bash
cp .env.example .env          # HA_URL und HA_TOKEN eintragen
docker compose up -d --build
# UI: http://<host>:7790
```

Token erzeugen: HA → Profil → „Long-lived access tokens" → „Create token".

`HA_URL` je nach Setup:
- gleiche Compose-/Docker-Netzwerk-Umgebung: `ws://homeassistant:8123/api/websocket`
- HA auf dem Docker-Host: `ws://host.docker.internal:8123/api/websocket`
- feste IP: `ws://192.168.178.144:8123/api/websocket`

Läuft HA in einem anderen Compose-Projekt, den Smart Energy Agent an dessen
Netzwerk hängen (siehe auskommentierte `networks:`-Abschnitte in `docker-compose.yml`).

### B) Home-Assistant-Add-on (für HA OS / Supervised)

Repository-Ordner als lokales Add-on einbinden und über den Supervisor bauen.
Nutzt Ingress (`config.yaml`, `Dockerfile`) und den `SUPERVISOR_TOKEN`
automatisch – keine manuelle Token-/URL-Konfiguration nötig.

## Lokaler Lauf ohne Docker (Entwicklung)

```bash
pip install -r requirements.txt
export HA_URL=ws://192.168.178.144:8123/api/websocket
export HA_TOKEN=...        # long-lived token
export SEA_HISTORY_DB=./smart_energy_agent.db
python -m smart_energy_agent.main
# UI: http://localhost:7790
```

## Konfiguration (Umgebungsvariablen)

| Variable | Default | Bedeutung |
|---|---|---|
| `HA_URL` | `ws://localhost:8123/api/websocket` | HA-Websocket (Standalone) |
| `HA_TOKEN` | – | Long-Lived-Token (Standalone) |
| `SUPERVISOR_TOKEN` | – | automatisch im Add-on-Betrieb |
| `SEA_LOG_LEVEL` | `info` | debug/info/warning/error |
| `SEA_HISTORY_DB` | `/data/smart_energy_agent.db` | SQLite-Pfad (Volume) |
| `SEA_HISTORY_DAYS` | `90` | Aufbewahrung Rohdaten (Tage) |
