# Smart Energy Agent – Technisches Konzept

**Ein Home-Assistant-Add-on zur automatischen Erkennung, Konfiguration und tarif-/PV-optimierten Steuerung von Energieverbrauchern – mit dialogbasierter Mitsteuerung über Home Assistant.**

Version 0.1 (Entwurf) · Stand: 29.05.2026

---

## 1. Ausgangslage und Zielsetzung

Neben dem bestehenden Add-on **SmartHub** (Python/asyncio-Add-on, das die Habitron-Hardware über Serial anbindet und per Websocket `ws://supervisor/core/websocket` mit Home Assistant kommuniziert) soll ein zweites Add-on **Smart Energy Agent** entstehen. Während SmartHub die Geräteanbindung verantwortet, agiert Smart Energy Agent als eine Ebene darüber: als **energiebezogener Optimierungs- und Steuer-Agent für die gesamte Home-Assistant-Instanz**, nicht nur für Habitron-Geräte.

Die Kernziele:

1. **Automatische Erkennung** aller Entitäten, die einen Energiebezug haben – typischerweise Leistungssensoren (W/kW), aber auch Energiezähler (kWh), schaltbare Verbraucher (Steckdosen, Relais), regelbare Verbraucher (Dimmer, Heizstäbe, Wallbox) sowie Quellen (PV-Erzeugung, Batterie, Netzbezug/-einspeisung).
2. **Konfigurierbare Einbeziehung**: Der Nutzer entscheidet pro Entität, ob und wie sie berücksichtigt wird (nur Monitoring, automatisch steuerbar, dialoggesteuert, ausgeschlossen).
3. **Optimale Steuerung** der Verbraucher in Abhängigkeit von dynamischem Stromtarif, PV-Verfügbarkeit, PV-/Wetterprognose, Verbrauchsprognose, Batterieladezustand, Netzlimits und nutzerdefinierten Komfort-/Zeitvorgaben.
4. **Prognosegestützte Vorschläge**: Wetter-/PV-Ertragsprognose und eine aus der **Historie gelernte Eigenverbrauchsprognose** bilden gemeinsam die Grundlage, um vorausschauend sinnvolle Maßnahmen zu planen und vorzuschlagen – nicht nur auf den Ist-Zustand zu reagieren.
5. **Dialogsteuerung über HA**: Für Verbraucher, die nicht vollautomatisch laufen sollen (z. B. Waschmaschine, Trockner, Geschirrspüler), fragt der Agent aktiv nach – „Jetzt günstig/PV-Überschuss vorhanden – Waschmaschine starten?" – und reagiert auf die Antwort.

### 1.1 Abgrenzung des Verantwortungsbereichs (Scope)

**Außerhalb des Scope:** Die Anbindung der PV-Wechselrichter und des Hausspeichers an Home Assistant ist **nicht** Aufgabe von Smart Energy Agent. Diese Hardware wird – wie alle anderen Geräte auch – über die jeweiligen, vorhandenen HA-Integrationen eingebunden (Herstellerintegration, Modbus, MQTT o. ä.). Smart Energy Agent setzt also voraus, dass PV-Erzeugung, Batterie-SoC/-Leistung, Netzbezug/-einspeisung und Verbraucher bereits als HA-Entitäten existieren.

**Innerhalb des Scope:** Smart Energy Agent *konsumiert* diese Entitäten. In einer **Einrichtungsphase** (Kapitel 4) erfasst und klassifiziert es sie vollständig, lässt den Nutzer Rollen und Steuermodi bestätigen und geht anschließend in einer **sinnvollen Priorisierung** mit ihnen um (Kapitel 6). Auch die Batterie wird dabei nur insoweit „gesteuert", wie ihre HA-Integration steuerbare Entitäten anbietet (z. B. Lade-/Entlade-Modus, Ziel-SoC); fehlt eine solche Steuerbarkeit, wird die Batterie ausschließlich gelesen und in der Priorisierung berücksichtigt.

Das Konzept orientiert sich bewusst an bewährten Mustern aus dem Open-Source-Umfeld (EVCC für PV-Überschuss-/Lastmanagement, Forecast.Solar für Ertragsprognose, dynamische Tarif-Integrationen wie Nord Pool / EPEX Spot / aWATTar / Tibber), ohne diese 1:1 zu kopieren – Smart Energy Agent soll diese Bausteine *nutzen bzw. ergänzen*, statt sie zu ersetzen.

---

## 2. Architekturentscheidung: Eigenständiges Add-on vs. SmartHub-Modul

Wie gewünscht werden beide Optionen gegenübergestellt.

### 2.1 Option A – Eigenständiges Home-Assistant-Add-on

Ein separates Add-on mit eigenem Container, eigenem Lebenszyklus und eigener Konfigurations-Oberfläche, das wie SmartHub über die HA-Websocket-API arbeitet.

**Vorteile**

- **Klare Domänentrennung.** SmartHub = Hardware-Anbindung (Habitron). Smart Energy Agent = herstellerübergreifende Energieoptimierung. Die Optimierung funktioniert auch in HA-Installationen, die *kein* Habitron einsetzen.
- **Unabhängiger Release-Zyklus.** Energie-Logik kann iterativ weiterentwickelt werden, ohne SmartHub-Releases zu riskieren (SmartHub ist sicherheitskritisch für die Hausgeräte).
- **Saubere Ressourcen-/Fehlerisolation.** Ein Absturz oder hoher CPU-Verbrauch der Optimierung (Solver, Prognose) beeinträchtigt SmartHub nicht.
- **Eigenes Berechtigungsprofil.** Smart Energy Agent braucht breiten Lesezugriff auf *alle* HA-States und Schreibzugriff (Service-Calls) auf viele Domänen – das ist ein anderes Sicherheitsprofil als SmartHubs gezielter Habitron-Zugriff.
- **Skalierbarkeit/Portabilität.** Lässt sich als eigenständiges Produkt vermarkten und auch ohne SmartHub einsetzen.

**Nachteile**

- **Doppelte Infrastruktur.** Websocket-Client, Auth/Token-Handling, Config-Webserver, Logging müssen (teils) neu aufgebaut werden – existiert in SmartHub bereits (`event_server.py`, `api_server.py`, `config_server.py`).
- **Zwei Konfigurationsoberflächen** für den Nutzer.
- **Koordinationsbedarf**, wenn beide Add-ons dasselbe physische Gerät steuern (z. B. ein Habitron-Relais), um Konflikte zu vermeiden.

### 2.2 Option B – Modul innerhalb von SmartHub

Smart Energy Agent als zusätzliches Submodul/Server innerhalb des SmartHub-Add-ons, das dessen vorhandene Websocket-Verbindung, Config-UI und Server-Infrastruktur mitnutzt.

**Vorteile**

- **Wiederverwendung vorhandener Bausteine**: Websocket-Anbindung an HA, Token-Handling, das Webserver-/Template-Gerüst (`web/*.html`), Logging-Setup, Settings-Persistenz.
- **Eine Oberfläche, eine Installation** für den Nutzer.
- **Direkter In-Process-Zugriff** auf Habitron-Modul-/Energiezählerdaten (EM230/EM24/EM25) ohne Umweg über HA-States – potenziell niedrigere Latenz und höhere Datenrate.
- **Einfachere Konfliktvermeidung** bei der Steuerung von Habitron-Geräten (gleicher Prozess kennt beide Seiten).

**Nachteile**

- **Vermischung zweier Domänen** in einer ohnehin großen Codebasis (SmartHub umfasst bereits zahlreiche je 30–90 kB große Module). Die Energie-Logik würde die Komplexität deutlich erhöhen.
- **Gekoppelte Releases und gemeinsames Risiko**: Fehler im Optimierer können das hardwarenahe SmartHub destabilisieren.
- **Beschränkung auf Habitron-Kontext**: Konzeptionell schwerer als eigenständiges, herstellerübergreifendes Produkt positionierbar.
- **Geteilte Ressourcen** (Event-Loop, CPU) – rechenintensive Optimierung konkurriert mit zeitkritischer Serial-Kommunikation.

### 2.3 Empfehlung

**Empfohlen wird Option A – ein eigenständiges Add-on – mit gezielter Wiederverwendung von SmartHub-Bausteinen und einer optionalen, schlanken Kopplung.**

Begründung: Die Energieoptimierung ist domänenfremd zur Serial-/Hardware-Logik von SmartHub und profitiert stark von eigenem Release-Zyklus, Fehlerisolation und herstellerübergreifender Reichweite. Die zeitkritische Serial-Kommunikation von SmartHub darf nicht mit dem rechenintensiven Optimierer (Prognose-Auswertung, Fahrplan-Berechnung) um denselben Event-Loop konkurrieren.

Die Nachteile (doppelte Infrastruktur) werden gezielt entschärft:

- **Code-Wiederverwendung statt Neuentwicklung**: Die bewährten Muster aus `event_server.py` (Websocket-Auth, Reconnect, `call_service`-Aufbau) werden als gemeinsame, kleine Bibliothek (`ha_ws_client`) extrahiert oder in Smart Energy Agent nachgebaut. Ebenso das Config-Webserver-Gerüst.
- **Optionale Kopplung zu SmartHub**: Smart Energy Agent steuert Habitron-Geräte grundsätzlich über die regulären HA-Entitäten (Service-Calls). Für Sonderfälle (hochfrequente Echtzeitmessung der EM-Zähler, abgestimmte Steuerung) kann eine schmale lokale HTTP-Schnittstelle zwischen den Add-ons definiert werden – aber nur additiv, nicht als harte Abhängigkeit.

> Der Rest des Konzepts beschreibt Smart Energy Agent als eigenständiges Add-on. Bei Wahl von Option B bleiben Datenmodell, Optimierungs- und Dialoglogik identisch; lediglich die Infrastrukturkapitel (3.x) würden in die SmartHub-Server eingebettet.

---

## 3. Systemarchitektur

### 3.1 Gesamtüberblick

Smart Energy Agent besteht aus sieben logischen Schichten, die intern als asyncio-Komponenten innerhalb eines HA-Add-on-Containers laufen:

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Home Assistant Core                          │
│   States · Services · Events ·  Notifications/Companion App · TTS     │
└───────────────▲───────────────────────────────────────▲─────────────┘
                │  Websocket (ws://supervisor/core/websocket)
                │  + REST (Supervisor)
┌───────────────┴───────────────────────────────────────┴─────────────┐
│                            Smart Energy Agent (Add-on)                       │
│                                                                       │
│  ┌─────────────┐   ┌──────────────┐   ┌───────────────────────────┐  │
│  │ HA-Connector│──▶│ Entity-       │──▶│  Config-/Geräteregister   │  │
│  │ (WS/REST)   │   │ Discovery     │   │  (Klassifizierung+Mapping)│  │
│  └─────────────┘   └──────────────┘   └─────────────┬─────────────┘  │
│         │                                            │                │
│         ▼                                            ▼                │
│  ┌─────────────┐   ┌──────────────┐   ┌───────────────────────────┐  │
│  │ State-/Mess-│   │ Forecast-     │   │  Optimierungs-Engine      │  │
│  │ Aggregator  │──▶│ Provider      │──▶│  (Planner/Scheduler)      │  │
│  │ (Live-Power)│   │ (PV/Tarif)    │   │                           │  │
│  └─────────────┘   └──────────────┘   └─────────────┬─────────────┘  │
│                                                       │                │
│                          ┌────────────────────────────┼──────────────┐│
│                          ▼                            ▼               ││
│                   ┌──────────────┐          ┌──────────────────────┐ ││
│                   │ Dialog-/     │          │ Control-Executor      │ ││
│                   │ Interaktions-│◀────────▶│ (Service-Calls,       │ ││
│                   │ Manager      │          │  Guards, Konfliktlogik)│ ││
│                   └──────────────┘          └──────────────────────┘ ││
│                          ▲                                            ││
│  ┌───────────────────────┴──────────────────────────────────────┐   ││
│  │  Web-UI / Config-Server (aiohttp)  +  Persistenz (SQLite/YAML) │   ││
│  └──────────────────────────────────────────────────────────────┘   ││
└───────────────────────────────────────────────────────────────────────┘
```

### 3.2 Komponenten im Detail

**(1) HA-Connector.** Persistenter Websocket-Client zur HA-Core-API (analog zu SmartHubs `event_server.py`: Auth via `SUPERVISOR_TOKEN`, automatischer Reconnect, Ping/Pong-Heartbeat). Aufgaben: `get_states` (Initial-Snapshot), `subscribe_events` für `state_changed` (Live-Updates), `call_service` (Steuerung), `get_services`/`config/entity_registry/list` und `config/device_registry/list` (Metadaten für Discovery). REST über Supervisor-Proxy nur als Fallback.

**(2) Entity-Discovery.** Klassifiziert alle Entitäten anhand von `device_class`, `unit_of_measurement`, `state_class`, Domain und Namensheuristiken (siehe Kapitel 4).

**(3) Config-/Geräteregister.** Persistente Zuordnung Entität → Rolle (Quelle/Verbraucher/Sensor), Steuermodus, Geräteparameter, Komfortregeln. Quelle der Wahrheit für die Optimierung.

**(4) State-/Mess-Aggregator.** Hält den aktuellen Energiezustand des Haushalts im Speicher: PV-Erzeugung, Netzbezug/-einspeisung, Batterie-SoC/-Leistung, Hausverbrauch, Einzelverbraucher-Leistungen. Berechnet abgeleitete Größen (verfügbarer PV-Überschuss = Erzeugung − Hausgrundlast − reservierte Lasten).

**(5) Forecast-Provider.** Zentrale Vorausschau-Komponente, die drei Zeitreihen für die nächsten 24–48 h liefert (Details Kapitel 5a): (a) **PV-/Wetter-Ertragsprognose**, (b) **Eigenverbrauchsprognose aus der Historie** und (c) **dynamischer Tarif**. Alle drei werden auf einheitliche Slots (15/60 min) normalisiert. Aus PV-Prognose minus Verbrauchsprognose ergibt sich die zentrale Planungsgröße: der **erwartete PV-Überschuss je Slot**.

**(6) Optimierungs-Engine (Planner).** Erstellt aus Zustand, Prognosen, Geräteparametern und Regeln einen **Fahrplan**: Welcher Verbraucher läuft wann, mit welcher Leistung? (Details Kapitel 6.)

**(7) Control-Executor & Dialog-Manager.** Setzt den Fahrplan über Service-Calls um bzw. löst für dialoggesteuerte Geräte Rückfragen an den Nutzer aus und verarbeitet dessen Antworten (Kapitel 7). Ein **Guard-Layer** verhindert unsichere/konfliktäre Schaltvorgänge.

**(8) Web-UI/Config-Server + Persistenz.** aiohttp-basierte Konfigurationsoberfläche (Muster wie SmartHubs `config_server.py` / `web/*.html`), erreichbar über das HA-Ingress-Panel. Persistenz in SQLite (Zeitreihen/Historie, Lernen) und YAML/JSON (Konfiguration).

### 3.3 Technologie-Stack

Bewusst nah an SmartHub, um Know-how und Muster wiederzuverwenden: **Python 3.12+, asyncio**, `aiohttp` (Web-/Config-Server), `websockets` (HA-Anbindung), `PyYAML` (Konfig), `SQLite` (`aiosqlite`) für Historie. Für die **Prognose** zunächst Baseline-Verfahren mit `numpy`/`pandas` (keine ML-Abhängigkeit), optional später leichtes ML (`scikit-learn` bzw. `LightGBM`/`XGBoost`) als Ausbaustufe (Kapitel 5a.4) – bewusst **kein** Deep Learning/LLM für die Vorhersage. Für die Optimierung zunächst eine **regelbasierte/heuristische Engine** (keine schwere Solver-Abhängigkeit); optional später `pulp`/`OR-Tools` für echte MILP-Optimierung. Paketierung als **HA-Add-on** (`config.yaml`, `Dockerfile`, `build.yaml`), Bereitstellung über ein Add-on-Repository, UI via **Ingress**.

---

## 4. Einrichtungsphase: Entitätserkennung und -klassifizierung

Die Einrichtung ist ein bewusst geführter, mehrstufiger **Onboarding-Assistent** (Wizard) und kein einmaliger Hintergrund-Scan. Ziel: einmalig ein vollständiges, korrektes Energiemodell des Haushalts aufbauen, das danach Grundlage aller Prognosen und Steuerentscheidungen ist.

### 4.0 Ablauf der Einrichtungsphase

1. **Voraussetzungs-Check.** Smart Energy Agent prüft, ob die nötigen HA-Integrationen vorhanden sind: mindestens eine PV-Erzeugung, Netzbezug/-einspeisung (Smart Meter), optional Batterie, eine Wetter-Entität sowie – falls genutzt – eine dynamische Tarifquelle. Fehlende Bausteine werden mit Hinweis markiert (z. B. „keine Batterie erkannt – Betrieb ohne Speicher möglich").
2. **Vollständige Erfassung.** Über die HA-Websocket-API werden alle States samt Entity-/Device-/Area-Registry geladen und klassifiziert (4.1/4.2).
3. **Zuordnung der Energiebilanz-Anker.** Der Nutzer bestätigt die zentralen Bilanzpunkte: Was ist PV-Erzeugung, was Netzbezug, was Einspeisung, was Batterie-SoC/-Leistung, was Gesamt-Hausverbrauch. Diese Anker sind Pflicht, weil sie die Energiebilanz und die Überschussberechnung tragen.
4. **Verbraucher erfassen und einordnen.** Steuerbare Lasten werden gelistet, mit Leistungssensoren verknüpft (4.2) und je Gerät ein Steuermodus (off/monitor/auto/dialog/scheduled), Leistungsdaten, Zeitfenster/Deadlines und Priorität gesetzt.
5. **Prognosequellen verbinden.** Wetter-/PV-Prognose und Tarifquelle werden zugeordnet (Kapitel 5).
6. **Lern-/Beobachtungsmodus.** Direkt nach der Einrichtung läuft Smart Energy Agent zunächst **rein beobachtend** (keine Schaltvorgänge), um Verbrauchshistorie für die Eigenverbrauchsprognose zu sammeln und die Erfassung im Realbetrieb zu validieren, bevor die Steuerung scharf geschaltet wird.

Die Einrichtung ist jederzeit wiederholbar/erweiterbar; neu in HA hinzugefügte Entitäten werden laufend erkannt und als „Neu entdeckt" zur Einordnung angeboten.

### 4.1 Datenquellen für die Erkennung

Home Assistant liefert über die Websocket-API alle nötigen Metadaten:

- **State-Attribute**: `device_class`, `unit_of_measurement`, `state_class`, `icon`.
- **Entity Registry** (`config/entity_registry/list`): `entity_id`, `platform`, `device_id`, `area_id`, `original_device_class`, `disabled_by`.
- **Device Registry** (`config/device_registry/list`): Hersteller, Modell, Geräte→Entitäten-Zuordnung.
- **Area Registry**: Raum-/Bereichszuordnung für sinnvolle Gruppierung.

### 4.2 Klassifikationslogik

Jede Entität wird in eine von mehreren **Energierollen** einsortiert. Die Erkennung kombiniert harte Kriterien (device_class/unit) mit Heuristiken:

| Rolle | Erkennungskriterien (Beispiele) | Beispiele |
|---|---|---|
| **Leistungssensor** | `device_class: power`, Einheit `W`/`kW` | PV-Leistung, Verbraucher-Leistung |
| **Energiezähler** | `device_class: energy`, Einheit `Wh`/`kWh`, `state_class: total_increasing` | Habitron EM230/EM24/EM25, Smart-Plug-Zähler |
| **PV-Erzeugung** | Power-Sensor + Name/Device enthält „pv", „solar", „inverter", „erzeugung"; positiv im Tagesverlauf | Wechselrichter-Output |
| **Netz (Bezug/Einspeisung)** | Power-Sensor mit Vorzeichen oder getrennte grid_import/grid_export-Sensoren | Smart Meter, Shelly 3EM |
| **Batterie** | `device_class: battery` (SoC %) + Lade-/Entladeleistungssensor | Hausspeicher |
| **Schaltbarer Verbraucher** | Domain `switch`, `light`, `input_boolean`; mit `turn_on`/`turn_off` | Steckdose, Relais, Heizstab |
| **Regelbarer Verbraucher** | Domain `number`/`light`(Helligkeit)/`climate`/`fan`/Wallbox-Stromstärke | Dimmer, Wallbox, Klima |
| **Smart Appliance** | Domain `sensor`/`select` mit Programmstatus (z. B. Home Connect) | Waschmaschine, Geschirrspüler |

**Verknüpfungsheuristik (wichtig):** Ein steuerbarer Verbraucher (z. B. Steckdose) wird – soweit möglich – mit „seinem" Leistungssensor verknüpft, über die gemeinsame `device_id` im Device Registry oder über Namens-/Bereichsähnlichkeit. So weiß der Agent, wie viel Leistung ein Schaltvorgang real bedeutet. Wo keine automatische Zuordnung gelingt, kann der Nutzer in der UI manuell verknüpfen oder eine **typische Leistung** hinterlegen.

### 4.3 Vorschlags- statt Automatik-Prinzip

Discovery erzeugt **Vorschläge**, keine fertige Konfiguration. Die UI präsentiert die erkannten Entitäten gruppiert (Quellen / steuerbare Verbraucher / reine Sensoren) mit vorausgewählter Rolle und einem Konfidenzhinweis. Der Nutzer bestätigt, korrigiert oder schließt Entitäten aus. Neuerkennungen (neu hinzugefügte HA-Entitäten) werden periodisch geprüft und als „Neu entdeckt" markiert.

---

## 5. Datenmodell

### 5.1 Kernobjekte (vereinfachtes Schema)

**EnergyEntity** – jede einbezogene HA-Entität
```
entity_id, friendly_name, role (source|consumer|sensor|storage),
ha_device_id, area, unit, device_class,
linked_power_entity (optional), linked_switch_entity (optional),
include (bool), confidence
```

**ManagedConsumer** – steuerbarer Verbraucher mit Energie-Steuerprofil
```
id, name, control_entity, power_entity,
control_mode: off | monitor | auto | dialog | scheduled
type: simple_switch | dimmable | ev_charger | heatpump | appliance | water_heater
nominal_power_w, min_power_w, max_power_w (für regelbare)
runtime: { typical_duration_min, deferrable (bool), interruptible (bool) }
constraints: {
   earliest_start, latest_finish,        # Zeitfenster (Komfort/Deadline)
   min_runtime_per_day, max_starts_per_day,
   min_off_time_min (Schaltschutz),
   required_kwh (z.B. EV-Ladeziel)
}
priority (1..10)
trigger_logic: {
   pv_surplus_threshold_w, price_threshold_ct, soc_min,
   combine: AND | OR
}
```

**EnergyState** – Live-Zustand (im Speicher, periodisch persistiert)
```
timestamp, pv_power_w, grid_import_w, grid_export_w,
battery_soc_pct, battery_power_w, house_load_w,
available_surplus_w, per_consumer_power
```

**Forecast** – Zeitreihen
```
pv_forecast[]      : {slot_start, expected_w, expected_wh, confidence}
load_forecast[]    : {slot_start, expected_base_load_w, confidence}   # Eigenverbrauch aus Historie
surplus_forecast[] : {slot_start, expected_surplus_w}                 # = pv - load (abgeleitet)
price_forecast[]   : {slot_start, price_ct_kwh, level (very_cheap..very_expensive)}
```

**Plan / Schedule** – Ergebnis der Optimierung
```
plan_id, created_at, horizon,
actions[]: {consumer_id, slot_start, slot_end, target_power_w, reason}
```

**DialogRequest** – offene Rückfrage an den Nutzer
```
id, consumer_id, question, created_at, expires_at,
options[], status (pending|answered|expired), answer, channel
```

### 5.2 Persistenz

- **Konfiguration** (Entitäten, ManagedConsumer, globale Settings): YAML/JSON-Datei(en) im Add-on-Datenverzeichnis – menschenlesbar, versionierbar, analog zu SmartHubs Settings-Dateien.
- **Zeitreihen/Historie** (EnergyState, Pläne, Dialog-Historie, gemessene Geräteleistungen für das Lernen typischer Profile): **SQLite**. Bewusst eigene, schlanke Historie statt Belastung des HA-Recorders, mit definierter Aufbewahrung (z. B. 90 Tage Roh, danach aggregiert). **Speicherort konfigurierbar** (interne SD vs. angebundene SSD/externer Datenträger), da die laufende Schreiblast medienkritisch ist – auf SD strikte Deckelung/Aggregation, auf SSD entspannter (siehe Hardware-Hinweis in 7.3).

---

## 5a. Prognose – Wetter/PV und historischer Eigenverbrauch

Prognose ist ein zentraler, eigenständiger Baustein: Erst die Vorausschau erlaubt es, *vorausschauend* sinnvolle Maßnahmen vorzuschlagen statt nur auf den Ist-Zustand zu reagieren. Es werden zwei Prognosen erzeugt und zu einer **Überschussprognose** verrechnet, die zusammen mit der Tarifprognose die Planung trägt.

### 5a.1 PV-/Ertragsprognose (Wetter)

Quelle der Wahl ist eine bereits in HA vorhandene Prognose-Integration, deren Werte Smart Energy Agent nur konsumiert:

- **Forecast.Solar** (Standortspezifisch, anlagenbezogen: Ausrichtung, Neigung, kWp) – liefert direkt erwartete Erzeugung je Stunde, ideal als Primärquelle.
- Wechselrichter-eigene Prognose (falls die Hersteller-Integration eine liefert) oder **Solcast** als Alternative.
- **Wetterprognose** (HA `weather`-Entität: Bewölkung, Globalstrahlung/UV, Temperatur) als Korrektur-/Fallback-Eingang.

Liegt nur eine generische Wetter-Entität ohne PV-Prognose vor, schätzt Smart Energy Agent die Erzeugung selbst: aus der **historischen Beziehung zwischen Wetterlage und gemessener PV-Leistung** (gelernt im Beobachtungsmodus) wird ein einfaches Modell gebildet (Bewölkung/Strahlung/Tageszeit/Jahreszeit → erwartete PV-Leistung). So funktioniert die Prognose auch ohne dedizierte Forecast-Integration, wird aber durch eine solche deutlich genauer.

### 5a.2 Eigenverbrauchsprognose aus der Historie

Der Grundverbrauch des Haushalts (alles außer den vom Agenten gesteuerten Lasten) wird aus der eigenen, im Beobachtungsmodus gesammelten Historie prognostiziert:

- **Profilbildung**: typischer Lastgang je **Wochentag × Tageszeit** (Slot-Median, robust gegen Ausreißer), getrennt nach Werktag/Wochenende und optional Saison. Start als gleitende Statistik über die letzten N Wochen.
- **Bereinigung**: bereits vom Agenten geschaltete steuerbare Lasten werden herausgerechnet, damit die Prognose den *unbeeinflussbaren Grundverbrauch* abbildet und es keine Rückkopplung gibt.
- **Verfeinerung (Ausbaustufe)**: Berücksichtigung von Wetter (Temperatur → Wärmepumpe/Klima), Anwesenheit (HA `person`/Presence) und besonderen Tagen (Feiertage). Methodisch bewusst schlank (gleitende Mediane/saisonale Naive-Prognose, optional Gradient-Boosting), ohne schwere ML-Abhängigkeit.
- **Konfidenz**: jede Prognose trägt ein Unsicherheitsmaß; bei dünner Datenlage (kurz nach Einrichtung) bleibt der Agent konservativ und greift auf einfache Defaults zurück.

### 5a.3 Verrechnung und Nutzung

```
erwarteter_Überschuss(slot) = PV_Prognose(slot) − Eigenverbrauchsprognose(slot)
```

Diese Überschussprognose plus die Tarifprognose sind die zentralen Eingänge der Optimierung (Kapitel 6): Sie beantworten Fragen wie „Reicht der heutige PV-Überschuss am Nachmittag für einen Waschgang?", „Lohnt es, die Batterie morgens noch aus dem Netz nachzuladen, bevor die Preise steigen?" oder „Wann liegt das günstigste 2-Stunden-Fenster für den Trockner?". **Prognosegüte wird laufend gemessen** (Prognose vs. Ist) und für Selbstkorrektur und UI-Transparenz protokolliert.

### 5a.4 Modell- und Bibliotheksempfehlung (KI/ML)

Leitprinzip: **erst eine erklärbare, ressourcenschonende Baseline – ML nur additiv und nur dort, wo es die Baseline nachweislich schlägt.** Bewusst kein Deep Learning und keine LLMs für die Zeitreihenprognose: Für einen Einzelhaushalt ist die Datenmenge zu klein, der Mehrwert gegenüber Gradient Boosting marginal und die Laufzeit-/Speicherlast für ein Add-on (häufig auf einem Raspberry Pi) unverhältnismäßig. Ein LLM ist allenfalls für die *sprachliche* Aufbereitung der Dialogvorschläge sinnvoll (Kapitel 7), nicht für die Vorhersage.

**PV-/Wetterprognose** – möglichst *nicht* selbst modellieren, sondern an Forecast.Solar/Solcast delegieren (über die jeweilige HA-Integration). Nur als Fallback ohne dedizierte Prognosequelle ein schlankes, lokal trainiertes Regressionsmodell `Bewölkung/Strahlung/Tageszeit/Saison → PV-Leistung` (z. B. `scikit-learn` Gradient Boosting oder lineare Regression auf der gelernten Wetter↔PV-Beziehung).

**Eigenverbrauchsprognose – gestuft:**

- **Stufe 1 (MVP, ohne ML):** saisonal-naive Prognose / Median des Lastgangs je Wochentag × Tageszeit-Slot. Robust, transparent, sofort ab Beobachtungsmodus verfügbar, braucht kaum Daten. Umsetzung mit Bordmitteln (`numpy`/`pandas`), keine zusätzliche Abhängigkeit.
- **Stufe 2 (Ausbau, leichtes ML):** Gradient-Boosting-Regression (`LightGBM` oder `XGBoost`, alternativ `scikit-learn` `HistGradientBoostingRegressor` ohne native Extra-Lib) mit Features Wochentag, Uhrzeit/Slot, Außentemperatur, Anwesenheit (`person`/Presence), Feiertag, ggf. gleitende Verbrauchsmittel. Fängt Effekte wie temperaturabhängige Wärmepumpe/Klima sauber ein. **Aktivierungskriterien:** ausreichende Historie (mehrere Wochen) **und** messbar bessere Güte als die Baseline (rollierender Backtest, z. B. MAE/MAPE je Slot).

**Architektur dazu:** Der Forecast-Provider kapselt austauschbare „Predictor"-Strategien hinter einer einheitlichen Schnittstelle (`predict(horizon) → Zeitreihe + Konfidenz`). Baseline und ML-Modell implementieren dieselbe Schnittstelle; eine **Modellauswahl per laufendem Backtest** entscheidet, welcher Predictor pro Größe aktiv ist (Champion/Challenger). Modelle werden lokal im Add-on-`/data` trainiert und versioniert; **Training asynchron/zeitgesteuert** (z. B. nächtlich), damit es die Echtzeitregelung nicht belastet. Empfohlene Zusatzabhängigkeiten bleiben minimal und optional – Stufe 1 kommt ohne aus; `LightGBM` o. Ä. wird erst mit Stufe 2 gebündelt.

---

## 6. Optimierungs- und Steuerlogik

### 6.1 Eingangsgrößen

Der Planner arbeitet zyklisch (z. B. alle 30–60 s für Echtzeit-Reaktionen, plus stündliche Voll-Neuplanung) auf Basis von:

- **Live-Zustand**: aktueller PV-Überschuss, Netzbezug/-einspeisung, Batterie-SoC, Hausgrundlast.
- **PV-Prognose** (heute/morgen) und **Tarifprognose** (24–48 h Preiszeitreihe).
- **Geräteprofilen und -constraints** (Laufzeit, Deadlines, Mindest-Energiemengen, Prioritäten, Schaltschutz).
- **Nutzer-Overrides** (manuelles Ein/Aus, „heute nicht", Boost).

### 6.2 Zielfunktion (konzeptionell)

Minimiere die **Energiekosten** über den Planungshorizont, unter Nebenbedingungen Komfort/Deadline und Gerätegrenzen:

```
min  Σ_t ( P_grid_import(t) · price(t) − P_grid_export(t) · feed_in_tariff )
s.t. Komfort-Zeitfenster, Mindestlaufzeiten, Ladeziele, Netzanschlusslimit,
     Batterie-Priorität, Schaltschutz, max. Starts/Tag
```

Sekundärziele (gewichtet/lexikografisch): Eigenverbrauchsquote maximieren, Schaltzyklen minimieren (Geräteschonung), CO₂-Intensität minimieren (falls Datenquelle vorhanden).

### 6.3 Zweistufiger Ansatz: Heuristik zuerst, Solver optional

**Stufe 1 – Regelbasierter Prioritäts-/Schwellwert-Scheduler (MVP).** Bewährt aus dem EVCC-Umfeld und robust:

1. **Quellen-/Senken-Hierarchie** (Standardpriorität, konfigurierbar): Hausgrundlast → Batterie laden (bis Mindest-SoC) → PV-Überschuss auf steuerbare Verbraucher nach Priorität → Netzeinspeisung als letztes.
2. **PV-Überschuss-Modus**: Verbraucher wird gestartet/moduliert, solange `available_surplus_w ≥ Schwellwert` über eine Mindestdauer (Hysterese gegen Flattern). Regelbare Verbraucher (Wallbox, Heizstab) folgen dem Überschuss stufenlos/stufig.
3. **Tarif-Modus**: Deadline-gebundene, verschiebbare Lasten (z. B. „EV bis 7 Uhr auf 80 %", „Boiler bis morgen früh warm") werden in die **günstigsten Slots** der Preisprognose gelegt, die das Ziel rechtzeitig erfüllen (Greedy: günstigste Stunden zuerst belegen, bis benötigte kWh gedeckt).
4. **Kombi-Logik** pro Gerät: `pv_surplus OR price < schwelle` (laufen, wenn Sonne *oder* billig) bzw. `AND` für strikte Sparvorgabe.
5. **Guards**: Mindest-Ein-/Auszeiten, max. Starts/Tag, Netzanschlusslimit, Batterie-Schutz.

**Stufe 2 – Optimierende Planung (Ausbaustufe).** Für mehrere konkurrierende, zeitverschiebbare Lasten mit Deadlines wird Stufe 1 suboptimal. Dann lohnt eine echte **Fahrplanoptimierung** über den 24-h-Horizont in 15-Minuten-Slots als gemischt-ganzzahliges lineares Programm (MILP, z. B. mit `pulp`/CBC oder OR-Tools): Entscheidungsvariablen = Geräteleistung je Slot, Zielfunktion = Kosten, Nebenbedingungen wie oben. Ergebnis ist ein optimaler Fahrplan; die Heuristik bleibt als Fallback/Echtzeitkorrektur zwischen den Neuplanungen aktiv.

### 6.4 Reaktion auf Abweichungen

Der Live-Aggregator vergleicht Plan und Realität. Bei Wolken (PV-Einbruch), unerwarteter Hauslast oder Nutzereingriff korrigiert der Echtzeit-Regler innerhalb des aktuellen Slots (z. B. Wallbox-Strom drosseln statt Netzbezug zu erzwingen) und stößt bei größeren Abweichungen eine Neuplanung an.

### 6.5 Sicherheit und Konfliktvermeidung

- **Steuerungshoheit klar definieren**: Smart Energy Agent steuert nur Entitäten, die explizit auf `auto`/`scheduled` stehen. Manuelle Nutzereingriffe haben Vorrang und setzen das Gerät temporär in einen `manual_override`-Zustand (für einstellbare Dauer).
- **Konflikt mit anderen Automationen / SmartHub**: Vor dem Schalten prüft der Guard den letzten Auslöser des State-Wechsels; fremdgesteuerte Geräte werden nicht überschrieben. Empfehlung, dass dieselbe physische Last nicht gleichzeitig von SmartHub-Automationen und Smart Energy Agent aktiv geregelt wird (UI-Warnung bei Überschneidung).
- **Fail-safe**: Bei Verbindungsverlust zu HA oder fehlenden Prognosedaten geht der Agent in einen sicheren Default (keine ungewollten Schaltvorgänge; laufende kritische Lasten bleiben unangetastet).

---

## 7. Dialogsteuerung über Home Assistant

Für Verbraucher, die nicht vollautomatisch starten sollen (Waschmaschine, Trockner, Geschirrspüler), arbeitet Smart Energy Agent **vorschlagend** statt schaltend.

### 7.1 Auslöser

Der Dialog-Manager beobachtet die Geräte im Modus `dialog`. Eine Rückfrage wird ausgelöst, wenn eine günstige Gelegenheit erkannt wird und das Gerät startbereit/beladen ist, z. B.:
- PV-Überschuss über Schwellwert für absehbar ausreichende Dauer (laut Prognose), oder
- Tarif tritt in ein „sehr günstig"-Fenster ein, oder
- Deadline rückt näher und ein günstiges Fenster ist das letzte sinnvolle.

Zustandserkennung „beladen/bereit": über Smart-Appliance-Sensoren (z. B. Tür zu / Programm gewählt) oder – bei einfachen Steckdosengeräten – über eine vom Nutzer bestätigte „bereit"-Markierung bzw. ohne Vorbedingung.

### 7.2 Dialogkanäle (mehrere, konfigurierbar)

1. **HA Companion App – Actionable Notification** (empfohlen): Push mit Aktionsbuttons „Jetzt starten" / „In 1 h" / „Heute nicht". Smart Energy Agent sendet via `call_service` an `notify.mobile_app_*` mit `actions` und lauscht auf das `mobile_app_notification_action`-Event.
2. **`input_*`-Helfer + Lovelace-Karte**: Der Agent setzt einen `input_select`/`input_boolean` („Smart Energy Agent-Vorschlag"), den eine Dashboard-Karte anzeigt; die Nutzerantwort wird als State-Change zurückgelesen.
3. **HA Assist (Sprache, dialogfähig)** – Sprachein- und -ausgabe über die HA-Voice-Pipeline (STT → Conversation-Agent → TTS). Dies ist der Kanal für *echte* Sprachdialoge (siehe 7.3), nicht nur Ansagen.
4. **HA Persistent Notification** als einfacher Fallback ohne Companion App.

### 7.3 Natürlichsprachlicher Dialog (LLM-gestützt)

Für die Sprachinteraktion reichen vorgefertigte Textbausteine nicht – der Nutzer soll frei formulieren und der Agent natürlich, situativ und mehrstufig antworten können. Smart Energy Agent registriert sich dafür als **Conversation-Agent in der HA-Assist-Pipeline** und nutzt ein **LLM als Sprach-Frontend** über einer fest geregelten Steuer-Logik.

**Klare Rollentrennung (wichtig):** Das LLM ist ausschließlich für *Sprachverständnis und Sprachformulierung* zuständig – nicht für Energieentscheidungen. Die eigentlichen Entscheidungen (was ist günstig, was darf laufen, was kostet was) trifft weiterhin die deterministische Optimierungs-Engine. Das LLM bekommt deren Ergebnisse als strukturierten Kontext und übersetzt zwischen Mensch und Maschine. So bleibt das Verhalten erklärbar und das LLM kann keine ungewollten Schaltaktionen „erfinden".

**Funktionsweise:**

- **Verstehen (NLU):** Freitext-/Sprachäußerungen des Nutzers werden vom LLM in strukturierte Intents + Parameter überführt, z. B. „Mach die Wäsche, wenn's grün ist" → `{intent: schedule_appliance, device: washing_machine, condition: pv_surplus}` oder „Warum schlägst du das jetzt vor?" → `{intent: explain_recommendation}`.
- **Antworten (NLG):** Aus dem aktuellen Energiezustand, der Prognose und dem Plan formuliert das LLM eine natürliche, knappe Antwort – inkl. *Begründung* („Heute ab 13 Uhr sind laut Prognose ~2,5 kW Überschuss für gut zwei Stunden zu erwarten – ideal für einen Waschgang. Soll ich starten?").
- **Grounding via Function/Tool-Calling:** Das LLM darf nur eine **eng definierte Menge an Werkzeugen** aufrufen, die der Agent bereitstellt (z. B. `get_energy_state()`, `get_forecast()`, `get_plan()`, `propose_action()`, `confirm_action()`, `defer_action()`). Steuerbefehle laufen immer über den **Guard-Layer** (Kapitel 6.5) und bei sicherheitsrelevanten Aktionen mit expliziter Bestätigung – nie direkt aus dem LLM-Output.
- **Kontextfenster:** Dem LLM wird pro Anfrage ein kompakter, aktueller Snapshot mitgegeben (Überschuss jetzt/Prognose, Preis-Level, offene Dialoge, Gerätezustände, Nutzerpräferenzen), damit Antworten faktenbasiert statt halluziniert sind.

**Modellbetrieb – lokal auf dem Raspberry Pi 5 (geteilte Zielplattform):** Das LLM läuft vollständig lokal auf dem Pi 5, ohne Cloud, ohne laufende Kosten, mit voller Datenhoheit. Entscheidend: Der Pi 5 ist **kein dedizierter LLM-Host** – auf ihm laufen parallel HA Core unter HA-OS, SmartHub und weitere Add-ons. CPU und RAM müssen geteilt werden; das LLM bekommt nur ein **kleines, fest begrenztes Budget**. Zudem führt der Pi 5 LLMs CPU-only aus (kein LLM-Nutzen durch AI-HAT+/Hailo): 1B-Modelle ~5–8 Token/s, 3B ~4–6 Token/s.

> **Hardware bewusst offen gehalten – 8 GB und 16 GB werden beide unterstützt.** Default ist ein **1B-Modell, das auf beiden Varianten läuft** (portabel, geringe Last). Bei **16 GB** kommt als **optionale Ausbaustufe** ein 3B-Modell (~2–3 GB) infrage, das sich neben HA-OS + SmartHub dauerhaft halten ließe. Unabhängig von der RAM-Größe bleiben **Dateispeicher** (Modell-File + Historie) und **CPU/Temperatur** (Token-Geschwindigkeit, Throttling) die bindenden Grenzen.
>
> **Gemessener Ist-Stand (Beta-System, 28.05.2026):** Pi 5 mit **~8 GB RAM** (7941 MB), davon 45,7 % belegt → ~4,3 GB frei; CPU 33,3 % Grundlast bei 2,4 GHz; Temperatur 65,5 °C; **Dateispeicher 57,8 GB zu 86,1 % belegt → nur ~8 GB frei**; HA 2026.5.4, Habitron 2.6.3. Auf diesem 8-GB-Stand ist das 1B-Modell der tragfähige Default; 16 GB schaffen zusätzlich Raum für die optionale 3B-Stufe. Der **Dateispeicher bleibt in beiden Fällen der knappste Faktor**, und die **Temperatur** mahnt weiter zu Thread-/Lastbegrenzung.

Daraus folgt eine **bewusst ressourcenschonende, Pi-5-taugliche Auslegung:**

- **Kleines Modell als Standard:** ein **1B-Instruct-Modell** (z. B. Gemma 3 1B, Llama 3.2 1B), Q4-quantisiert (~0,7–1 GB RAM), via `llama.cpp`/`ollama`. Ein 3B-Modell bleibt optional, ist aber wegen RAM- und CPU-Teilung mit HA/SmartHub nicht Default.
- **RAM-/CPU-Budget statt Maximalausnutzung:** feste Obergrenzen (z. B. begrenzte Thread-Zahl, niedrige Prozesspriorität/`nice`), damit die Token-Generierung die **zeitkritische Serial-Kommunikation von SmartHub und HA Core nicht ausbremst**.
- **Modell nur bei Bedarf laden (idle-unload):** Das Modell wird bei tatsächlichem Dialog geladen und nach Leerlauf wieder entladen, statt dauerhaft RAM zu belegen – passend dazu, dass Energiedialoge selten und nicht zeitkritisch sind.
- **Dateispeicher schonen / Speichermedium:** Auf dem Beta-System sind nur ~8 GB Disk frei. Daher kleines Modell-File (~1 GB), keine mehreren Modelle parallel vorhalten, und die SQLite-Historie hart deckeln/aggregieren. Da die Historie laufend schreibt, ist das Medium kritisch: Eine **NVMe-SSD über das M.2-/PCIe-HAT des Pi 5** wird empfohlen (mehr Platz, deutlich höhere Schreibhaltbarkeit und Geschwindigkeit, schnelleres Modell-Laden); eine **128-GB-SD** ist die günstige Minimallösung, dann aber mit konsequent begrenzter Schreiblast (gedeckelte Historie). Die Persistenzschicht (Kapitel 5.2) wird so ausgelegt, dass der Speicherort (SD/SSD/extern) konfigurierbar ist.
- **Temperatur/Throttling:** Bei bereits ~65 °C im Leerlauf erzeugt anhaltende Token-Generierung Wärme. Thread-Begrenzung, `nice` und ausreichende Kühlung (aktiv) verhindern, dass Dauerlast HA/SmartHub durch Throttling beeinträchtigt.
- **Hybrid statt Vollgenerierung:** Antworttexte überwiegend aus knappen, mit Live-Daten gefüllten Vorlagen; das LLM formuliert nur dort frei, wo nötig. Minimiert Token-Last und Antwortzeit.
- **Asynchrone Entkopplung:** LLM-Aufrufe laufen strikt außerhalb des Regel-/Steuerpfads; die Energieregelung wartet nie auf das Modell.
- **Fallback ohne LLM:** Bei Ressourcenknappheit, fehlender Conversation-Pipeline oder Überlast fällt der Dialog automatisch auf intent-basierte Sätze und Button-Notifications (7.2) zurück. Sprachdialog ist Komfort-Upgrade, keine harte Voraussetzung.

**Sicherheits-/Qualitätsleitplanken:** striktes Tool-Whitelisting, Bestätigungszwang vor schaltenden Aktionen, Plausibilitätsprüfung der LLM-Parameter gegen die reale Gerätekonfiguration, Begrenzung auf den Energie-Domänenkontext (kein offener Chatbot), sowie Protokollierung jeder LLM-vermittelten Aktion mit Begründung in der Historie.

### 7.4 Dialog-Lebenszyklus

```
Gelegenheit erkannt
   → DialogRequest erzeugt (status=pending, expires_at gesetzt)
   → Notification/Helper/Sprachausgabe an gewählten Kanal
   → Nutzerantwort (Button ODER Freitext/Sprache → LLM-Intent) | Timeout
        ├─ "Jetzt starten"  → Control-Executor schaltet (sofern schaltbar)
        │                      oder bestätigt manuellen Start
        ├─ "In X / zu Zeitpunkt" → als scheduled-Action in Plan aufnehmen
        ├─ "Heute nicht"    → Gerät für Tag pausieren, nicht erneut fragen
        ├─ Rückfrage ("warum?", "wie viel spare ich?") → LLM erklärt aus Plan/Prognose,
        │                      Dialog bleibt offen
        └─ Timeout/expired  → keine Aktion; ggf. später erneut anbieten
   → Ergebnis + Begründung in Historie (für Transparenz & Lernen)
```

Sprach-/Freitextantworten werden über den LLM-Conversation-Agent (7.3) in genau diese Zweige aufgelöst; schaltende Aktionen erfordern weiterhin Bestätigung und laufen über den Guard-Layer.

**Anti-Nerv-Regeln**: Pro Gerät max. N Rückfragen/Tag, Ruhezeiten (z. B. nicht nachts), keine Wiederholung nach „Heute nicht", Bündelung mehrerer Vorschläge in eine Benachrichtigung.

### 7.5 Optionales Lernen

Aus der Dialog-Historie und den gemessenen Laufzeiten kann Smart Energy Agent typische Nutzungsmuster ableiten (wann startet der Nutzer üblicherweise welche Geräte, wie lange laufen sie real) und damit bessere Vorschlagszeitpunkte und Laufzeitschätzungen für die Optimierung gewinnen. Start als einfache Statistik (Median je Wochentag/Uhrzeit), keine schwere ML-Abhängigkeit.

---

## 8. Konfigurationsoberfläche (Web-UI)

Über HA-**Ingress** erreichbar (eigenes Seitenpanel), aiohttp-basiert nach dem Muster von SmartHubs `config_server.py` und den HTML-Templates unter `web/`. Geplante Ansichten:

1. **Dashboard**: Live-Energiefluss (PV, Netz, Batterie, Hauslast), aktueller Tarif/Preisverlauf, PV-Prognose, geplante Aktionen, offene Dialoge.
2. **Geräteerkennung**: erkannte Entitäten gruppiert, Rolle bestätigen/ändern, Verknüpfung Schalter↔Leistungssensor, ein-/ausschließen.
3. **Verbraucher-Konfiguration**: pro ManagedConsumer Steuermodus, Leistungsdaten, Zeitfenster/Deadlines, Prioritäten, Schwellwerte, Schaltschutz.
4. **Tarif & Prognose**: Auswahl/Anbindung der Preis- und PV-Prognosequellen, Einspeisevergütung, statische Tarife.
5. **Regeln & Strategie**: globale Priorisierung (Batterie vs. Verbraucher), Optimierungsziel (Kosten/Eigenverbrauch/CO₂), Dialog-Kanäle und -Limits.
6. **Historie & Auswertung**: Verlauf, erzielte Einsparungen/Eigenverbrauchsquote, Dialogprotokoll.

Zusätzlich exponiert Smart Energy Agent eigene **HA-Entitäten** (über die Websocket-/Service-API bzw. MQTT-Discovery), damit Status und Kennzahlen auch in normalen HA-Dashboards und Automationen nutzbar sind: z. B. `sensor.energyagent_available_surplus`, `sensor.energyagent_current_price_level`, `binary_sensor.energyagent_pending_dialog`, `sensor.energyagent_planned_savings_today`.

---

## 9. Add-on-Paketierung und Deployment

Als HA-Add-on (analog zu SmartHub im „Smart Center"-Modus mit `SUPERVISOR_TOKEN`):

- **`config.yaml`**: Add-on-Metadaten, `ingress: true`, benötigte Optionen (Prognose-/Tarif-Quellen, Standard-Steuermodi), `hassio_api`/`homeassistant_api` und passende Berechtigungen für States/Services.
- **`Dockerfile` + `build.yaml`**: Multi-Arch (aarch64/amd64) auf Basis der HA-Add-on-Base-Images; Python-Abhängigkeiten via `requirements.txt`.
- **Bereitstellung** über ein eigenes Add-on-Repository (GitHub), versioniert; CI-Build analog zu SmartHubs `.github/workflows`.
- **Datenhaltung** im persistenten Add-on-`/data`-Verzeichnis (SQLite + YAML), sauberes Backup/Restore über die HA-Snapshot-Mechanik.

---

## 10. Schnittstelle zu SmartHub (optional)

Grundsatz: **Smart Energy Agent steuert Habitron-Geräte über reguläre HA-Entitäten** – keine harte Kopplung nötig. Für Sonderfälle ist eine schmale, optionale lokale HTTP-/JSON-Schnittstelle vorgesehen:

- **Lesen**: hochfrequente Messwerte der EM230/EM24/EM25-Energiezähler direkt von SmartHub (geringere Latenz als der Umweg über HA-State-Updates), falls für die Echtzeitregelung benötigt.
- **Abstimmung**: Austausch eines „belegt"-Flags, damit dieselbe physische Last nicht gleichzeitig von SmartHub-Automationen und Smart Energy Agent geregelt wird.

Diese Kopplung ist additiv: Fehlt SmartHub, läuft Smart Energy Agent vollständig über die HA-API weiter.

---

## 11. Umsetzungs-Roadmap

**Phase 0 – Gerüst (1–2 Wochen).** Add-on-Skelett, HA-Websocket-Connector (Auth/Reconnect/Heartbeat, aus SmartHub-Mustern), Ingress-Webserver, Persistenz-Grundlage. Liest und zeigt alle HA-States.

**Phase 1 – Einrichtungsphase, Discovery & Monitoring (MVP-Basis).** Geführter Onboarding-Assistent, Entitätsklassifizierung, Zuordnung der Energiebilanz-Anker (PV/Netz/Batterie/Hausverbrauch), Geräteregister-UI, Live-Energiefluss-Dashboard, eigene Status-Sensoren in HA. Start des Beobachtungsmodus zum Sammeln der Verbrauchshistorie. *Noch keine Steuerung* – reines, vertrauensbildendes Monitoring.

**Phase 2 – Prognose.** Anbindung der Wetter-/PV-Ertragsprognose (Forecast.Solar/Solcast/Wetter-Entität) und Aufbau der historiebasierten Eigenverbrauchsprognose; Verrechnung zur Überschussprognose, Prognosegüte-Messung und -Anzeige. Liefert die Grundlage für alle vorausschauenden Maßnahmen.

**Phase 3 – PV-Überschuss-Steuerung.** Live-Aggregator, regelbasierter Schwellwert-/Prioritäts-Scheduler, Steuerung einfacher schaltbarer und erster regelbarer Verbraucher, Guards/Schaltschutz, manuelle Overrides.

**Phase 4 – Tarif-Optimierung.** Anbindung dynamischer Tarifquellen, deadline-gebundene Lastverschiebung (günstigste Slots), Batterie-Priorisierung, prognosegestützte Kombi-Logik PV+Tarif.

**Phase 5 – Dialogsteuerung.** Zunächst Actionable Notifications, Dialog-Lebenszyklus, Anti-Nerv-Regeln, Button-/Helper-Kanäle. Anschließend der **LLM-gestützte Sprachdialog** (7.3): Registrierung als HA-Assist-Conversation-Agent, Tool-/Function-Calling über den Guard-Layer, **lokales kleines LLM auf dem Pi 5** (1–3B via `llama.cpp`/Ollama) mit Hybrid-Texten, Fallback auf Intent-Sätze.

**Phase 6 – Optimierende Planung & Lernen.** MILP-Fahrplanoptimierung als Ausbaustufe, Nutzungsmuster-Lernen, Auswertungen/Einsparberichte.

**Querschnitt durchgehend:** Sicherheit/Fail-safe, Konfliktvermeidung, Tests (inkl. Simulationsmodus mit eingespielten PV-/Tarif-Zeitreihen ohne reale Schaltvorgänge), Dokumentation.

---

## 12. Offene Punkte / nächste Entscheidungen

- **Tarifquelle(n)**: Welcher Anbieter ist real im Einsatz (Tibber, aWATTar, EPEX Spot, Nord Pool, statischer HT/NT)? Bestimmt die primäre Integration in Phase 4.
- **Vorhandene HA-Integrationen für PV/Batterie/Smart Meter**: Welche Entitäten liefern sie konkret (Erzeugung, SoC, Lade-/Entladeleistung, Netzbezug/-einspeisung), und bietet die Batterie-Integration *steuerbare* Entitäten (Lademodus/Ziel-SoC) – oder ist sie nur lesbar? Bestimmt, wie aktiv die Batterie in die Priorisierung eingebunden werden kann.
- **Wetter-/PV-Prognosequelle**: Ist Forecast.Solar/Solcast eingebunden oder nur eine generische `weather`-Entität (dann eigenes Schätzmodell, Kapitel 5a.1)?
- **Verbrauchshistorie**: Wie lange reicht die vorhandene HA-Recorder-Historie zurück (für einen schnelleren Start der Eigenverbrauchsprognose)?
- **Vorhandene steuerbare Lasten**: konkrete Erstkandidaten (Wallbox? Heizstab/Boiler? Wärmepumpe? Smart-Appliances mit Programmstatus?).
- **EVCC bereits im Einsatz?** Falls ja, sollte Smart Energy Agent die EV-Ladung an EVCC delegieren statt sie zu duplizieren und sich auf die übrigen Lasten + Gesamtkoordination konzentrieren.
- **Verhältnis zum HA-Recorder**: eigene SQLite-Historie (empfohlen) vs. Nutzung vorhandener HA-Statistiken/Long-Term-Statistics als Startdatenbasis.
- **Sprachdialog/LLM**: festgelegt auf **lokales 1B-LLM auf dem geteilten Pi 5** (`llama.cpp`/Ollama, Hybrid-Texte, idle-unload, gedeckeltes CPU-/RAM-Budget neben HA-OS + SmartHub). Beta-System hat **~8 GB RAM** (≈4,3 GB frei) und **nur ~8 GB freien Dateispeicher** – Disk ist der Engpass. **Hardware bewusst offen gehalten:** 1B-Default läuft auf 8 **und** 16 GB; 3B ist optionale Ausbaustufe bei 16 GB. Offen: (a) konkretes 1B-Modell (Gemma 3 1B vs. Llama 3.2 1B), (b) Speichermedium/-ort: NVMe-SSD über M.2-HAT (empfohlen für Schreibhaltbarkeit/Platz) vs. 128-GB-SD (Minimallösung mit gedeckelter Historie), (c) ist die HA-Assist-Voice-Pipeline (STT/TTS) bereits eingerichtet?

---

### Quellen / Referenzen

- [Nord Pool – Home Assistant Integration](https://www.home-assistant.io/integrations/nordpool/)
- [EPEX Spot and Awattar Electricity Prices – HA Community](https://community.home-assistant.io/t/epex-spot-and-awattar-electricity-prices/519151)
- [Unified dynamic electricity prices provider for HA – HA Community](https://community.home-assistant.io/t/unified-dynamic-electrity-prices-provider-for-ha/823457)
- [evcc – Smart Charging / Energy Management](https://evcc.io/en/)
- [evcc – Solar Surplus Charging (Docs)](https://docs.evcc.io/en/features/solar-charging/)
- [Home Assistant gesteuert von evcc (PV-Überschuss)](https://github.com/marq24/ha-evcc/blob/main/HA_CONTROLLED_BY_EVCC.md)
- [Smart Home Energy Management 2026 – ioBroker & Home Assistant](https://energie.werner.solutions/en/blog/smart-home-energy-management-austria-2026-save-up-to-900-with-iobroker-home-assistant/)
