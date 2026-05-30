# Smart Energy Agent

Energy-aware detection, monitoring and (later) tariff-/PV-optimized control for
Home Assistant. Current status: Phase 1 – monitoring and curation.

Concept (German): see [`EnergyAgent_Konzept.md`](EnergyAgent_Konzept.md).

## Usage (Phase 1)

The UI shows the detected entities grouped by energy role. Config/diagnostic
entities (LEDs, flags, setpoints, device battery levels) are hidden
automatically. Measurements (PV/grid/battery/power/energy) are included by
default, controllable candidates (switches/lights/climate) are not — enable
those with the checkbox. Use the role dropdown to fix a misclassification.
The selection is stored in `overrides.json` (next to the history DB, usually
`/data`) and survives restarts.

The "included only / all candidates" toggle reveals the not-yet-included
suggestions.

## Test environment without real PV/battery

If the test HA has no PV/storage system, the template sensors in
[`examples/ha_sim_sensors.yaml`](examples/ha_sim_sensors.yaml) simulate a
realistic daily profile (PV, house load, battery SoC/power, grid). See the file
header for how to include them.

## Two run modes

### A) Standalone container (for HA Container / development)

Recommended when HA itself runs as a container (no Supervisor, no add-ons).
Smart Energy Agent connects via websocket + long-lived token.

```bash
cp .env.example .env          # set HA_URL and HA_TOKEN
docker compose up -d --build
# UI: http://<host>:7790
```

Create a token: HA → profile → "Long-lived access tokens" → "Create token".

`HA_URL` depending on your setup:
- same compose/docker network: `ws://homeassistant:8123/api/websocket`
- HA on the docker host: `ws://host.docker.internal:8123/api/websocket`
- fixed IP: `ws://192.168.178.144:8123/api/websocket`

If HA runs in another compose project, attach Smart Energy Agent to its network
(see the commented `networks:` sections in `docker-compose.yml`).

### B) Home Assistant App / add-on (for HA OS / Supervised)

Add the repository as an app repository and install via the Supervisor. Uses
Ingress (`config.yaml`, `Dockerfile`) and the `SUPERVISOR_TOKEN` automatically —
no manual token/URL configuration needed. Publishing is automated, see
[`DEPLOYMENT.md`](DEPLOYMENT.md).

## Local run without Docker (development)

```bash
pip install -r requirements.txt
export HA_URL=ws://192.168.178.144:8123/api/websocket
export HA_TOKEN=...        # long-lived token
export SEA_HISTORY_DB=./smart_energy_agent.db
python -m smart_energy_agent.main
# UI: http://localhost:7790
```

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `HA_URL` | `ws://localhost:8123/api/websocket` | HA websocket (standalone) |
| `HA_TOKEN` | – | long-lived token (standalone) |
| `SUPERVISOR_TOKEN` | – | provided automatically when run as an app |
| `SEA_LOG_LEVEL` | `info` | debug/info/warning/error |
| `SEA_HISTORY_DB` | `/data/smart_energy_agent.db` | SQLite path (volume) |
| `SEA_HISTORY_DAYS` | `90` | raw history retention (days) |
