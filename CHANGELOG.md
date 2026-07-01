# Changelog

## 0.8.1

- **Grundeinstellungen** sind jetzt in **ein-/ausklappbare Karten** gegliedert (Allgemein,
  PV-Überschuss & Speicher, Tarife, Status) — wie die Strategien.
- **Komponenten**: jede Kategorie (Netz, Verbraucher, …) ist eine **klappbare Karte**, die ihre
  Geräte **und** den „Hinzufügen"-Button enthält.
- Die **Seitenleiste bleibt beim Scrollen stehen** (fixiert unter dem Kopfbereich).
- Die **Strategie-Auswahl** wirkt jetzt als **Voreinstellung**: sie setzt die Master-Schalter
  (PV-Überschuss immer an; Tarif + Optimierer an bei Hybrid/Kosten, aus bei Eigenverbrauch/
  Autarkie) — danach frei feinjustierbar.
- **Reserve-SoC der Batterie** ist jetzt immer einstellbar (nicht mehr hinter „Tarif-Netzladen"
  versteckt); „Batterie-Mindest-SoC" heißt zur Klarheit **„Lade-Vorrang bis SoC"** (das ist die
  Lade-Vorrang-Schwelle, nicht die Entlade-Reserve).
- **Batterie-Tarif vereinheitlicht**: das einfache Schwellen-Netzladen läuft nur bei aktivem
  Tarif und nur, wenn der Prognose-Optimierer aus ist (sonst hat der Optimierer Vorrang).
- **Hilfe-Button (ⓘ)** mit Beschreibung für **jede** Strategie.
- **Einsparungsseite** überarbeitet: Ersparnis wird **grün**, Mehrkosten **rot** dargestellt
  (Betrag ohne Vorzeichen, Beschriftung „Ersparnis vs. Baseline" bzw. „Mehrkosten vs. Baseline").
  Die Erklärung der Vergleichs-Baselines liegt jetzt im **ⓘ-Popup** neben dem Auswahlfeld.
- **Einsparungsseite** sauber ausgerichtet: Zeitraum-Auswahl, Baseline-Selektor und die Felder
  darunter stehen in einem Raster gleicher Breite untereinander.
- Neue Baseline **„Dynamischer Tarif (Potenzial mit Speicher)"** mit realistischer
  Batterie-Simulation: drei Preisstufen (Hoch 17–21 Uhr / Niedrig 0–6 Uhr / Normal), eine Batterie
  der **einstellbaren Größe** wird über die echte Zeitreihe gerechnet (lädt PV-Überschuss, deckt
  den Verbrauch, lädt nachts günstig nach, entlädt in der Abendspitze). **Speichergröße** und
  **PV-Spitzenleistung** sind mit der aktuellen Anlage vorbelegt — durch Anpassen sieht man das
  Potenzial einer größeren Batterie bzw. eines PV-Ausbaus (Ertrag per Dreisatz skaliert).
- **PV-Überschuss-Senke**: zusätzlich zur Leistungsgrenze jetzt eine **Tages-Energiegrenze** (kWh)
  einstellbar (eigener Speicher der Senke, z.B. Warmwassertank/Auto-Akku); Auswahl per Selektor
  (Heizstab/Auto/Eigene).
- **Einsparungsseite** neu gegliedert: Zeitraum und Baseline-Selektor (in einer Karte mit dem
  ⓘ-Button) nutzen die volle Breite; die Prämissen (Tarif, Speicher, PV bzw. Senke) stehen jeweils
  in einer eigenen Zeile.
- **Modulierende Lasten ruhiger geregelt**: der Überschuss für die Modulation wird jetzt geglättet
  (neue Einstellung „Modulation glätten (s)", Standard 120 s, 0 = aus). Kurze Haus-/Batterie-Spitzen
  takten einen Heizstab nicht mehr auf 0 und wieder hoch.
- **Robust gegen nicht-synchrone Messwerte**: eine modulierende Last wird erst abgesenkt, wenn ein
  Defizit über mehrere Zyklen bestätigt ist (Entprellen); solange Überschuss/Einspeisung anliegt,
  bleibt sie an (Export-Schutz). Ein einzelner „verrutschter" Messpunkt wirft sie nicht mehr ab.
- **Sensor-Update-Rate**: auf der Seite „Geräte" steht jetzt hinter jeder Entität, wie oft sie
  aktualisiert (z. B. „(alle ~2 s)"), und veraltete Werte werden markiert.
- **Zeitliche Angleichung (neu, Grundeinstellungen „Sensoren zeitlich angleichen")**: nicht-synchrone
  Leistungssensoren werden vor der Bilanz zeitlich ausgerichtet — die schnellen über die Kadenz des
  langsamsten gemittelt, der langsamste hält seinen Wert. Basis für **Energiefluss-Anzeige und
  Regelung** → ruhigeres, konsistenteres Bild.
- **Staleness-Gate**: ist ein Bilanz-Sensor (Netz/Batterie) deutlich veraltet, werden modulierende
  Lasten eingefroren, statt auf veraltete Daten zu reagieren.
- **Heizstab/modulierende Lasten**: Stopp-Bedingung (z. B. „Stopp bei ≥ 65 °C") kann jetzt mit
  **Hysterese (Δ)** versehen werden — Wiederanlauf erst unter (Schwelle − Δ). Verhindert das
  schnelle Takten am Limit (ruhige, lange Zyklen statt Sägezahn).
- **Verlauf**: der **Begrenzungs-Sensor** einer Last (z. B. die ELWA-Temperatur „Stopp bei") wird
  jetzt im Detail-Plot mit dargestellt.
- **PV-Überschuss-Modulation**: regelt nun auf die **gemessene Ist-Leistung** der Last (statt des
  kommandierten Sollwerts) — sauberes Einschwingen ohne Überschwingen während der Rampe.
- **Einsparung (dynamische Baseline)**: Netz-Nachladen im Niedrig-Fenster nur noch, wenn es sich
  nach Wirkungsgrad-Verlusten lohnt (kein Verlustkauf bei flachem Tarif → größerer Speicher
  verschlechtert das Ergebnis nicht mehr).
- **Verlauf**: feste Zeiträume sind jetzt **kalender-ausgerichtet** (Tag 0–24 Uhr, Woche ab Montag,
  Monat ab dem 1., Jahr ab 1. Januar) statt rollierend ab „jetzt". ◀ ▶ blättert exakt einen
  Zeitraum weiter (z.B. einen Monat zurück). Neuer Button **„jetzt"** lässt den frei eingegebenen
  Zeitraum am aktuellen Zeitpunkt enden (z.B. letzte 24 h / 30 / 365 Tage).

## 0.7.0

- Neu: Dashboard-Panel **„Aktive Steuerung"** — zeigt live, welcher Regler gerade was
  und warum schaltet. Peak-Shaving, Einspeise-Limit, Notstrom-Reserve und Batteriepflege
  erscheinen jetzt mit Status auf der **Strategieseite**.
- Neu: **Regel-Editor** (JSON-logic) auf der Strategieseite; **Batterie-Kapazität** und
  optionale **PV-Abregel-Entität** konfigurierbar. Tageswerte (Mindestmenge, Pflege)
  überstehen jetzt Neustarts; der Optimierer nutzt die **Preis-Prognose** dynamischer Tarife.

- Neu: **Einspeise-Limit** — übersteigt die Einspeisung eine Grenze (W), wird die
  Batterie zwangsgeladen, um darunter zu bleiben.
- Neu: **Notstrom-Reserve (%)** — die Batterie wird auf diesen Ladezustand gehalten/
  geladen und von keiner Strategie darunter entladen (Backup für Stromausfall).
- Neu: **Zeitfenster-Peak-Shaving** — pro Tageszeit eigene Netzbezugs-Grenzen.
- Neu: **Batteriepflege** — periodische Vollladung (alle N Tage) zur SoC-Kalibrierung.

- UI: Der Menüpunkt „Erzeuger/Verbraucher" heißt jetzt **„Komponenten"** und enthält
  zusätzlich die **Fahrzeug-Verwaltung** (die separate Seite „Fahrzeuge" entfällt) —
  alle Anlagen-Komponenten an einem Ort.

- Neu: **Wärmepumpe SG-Ready** — zwei Relais (4 Zustände: normal / Anhebung / Zwang /
  Sperre) werden aus dem PV-Überschuss (und teurem Tarif) angesteuert.

- Neu: **Mindest-Energie/Tag** für mehrstufige Heizstäbe — wird notfalls rechtzeitig
  aus dem Netz garantiert (Energie-Pendant zum „spätesten Start").

- Neu: **Prognose-Optimierer (Batterie)** — lädt/entlädt die Batterie vorausschauend
  nach PV-Prognose und Strompreis (günstig laden, teuer entladen) und netzoptimiert
  (kein Netzladen, wenn die PV den Speicher noch füllt). Auf der Strategieseite
  aktivierbar; Reserve/Peak-Shaving bleiben übergeordnet. Standardmäßig aus.

- Neu: **Fähigkeits-Modell** intern — Geräte werden über Fähigkeiten (Speicher,
  ladbar, schaltbar, Stufen …) angesprochen; ein späteres V2G-Fahrzeug nimmt damit
  automatisch an Reserve/Peak/Eigenverbrauch teil.

## 0.6.0

- Neu: **Mehrstufiger Heizstab** — bei Warmwasser/Wärmepumpe lassen sich mehrere
  Schalt-Stufen (Relais) zuordnen; sie werden je nach PV-Überschuss zu-/abgeschaltet
  (max. Leistung gleichmäßig verteilt), notfalls bis zum „spätesten Start" erzwungen.

- Neu: **Fahrzeuge** als eigene Objekte (Seite „Fahrzeuge", mehrere möglich), getrennt
  von der Wallbox. Je nach Fähigkeit: nur SoC (Stopp-Signal), ladbar (Wallbox lädt bis
  Ziel-SoC) oder bidirektional (V2H/V2G – nimmt wie ein Speicher an Eigenverbrauch,
  Peak-Shaving und Reserve teil). Wallbox-Ladestopp folgt dem verknüpften Fahrzeug.

- Neu: Auf der Seite „Verlauf" eine **Energiebilanz** (unter den Detail-Plots), die
  über den aktuell angezeigten Zeitraum gerechnet wird (inkl. Zoom/Pan): PV-Erzeugung,
  Hausverbrauch, Netzbezug/-einspeisung, Batterie geladen/entladen, Eigenverbrauch,
  Eigenverbrauchsquote und Autarkiegrad.

- UI: Auf der Geräte-Einstellungsseite („Erzeuger/Verbraucher") sind die Überschriften
  der ausklappbaren Geräte-Karten wieder schwarz.

- Fix: Die angezeigte Version entspricht jetzt der tatsächlich laufenden (vorher
  blieb „0.2.1" stehen) — sie wird zur Laufzeit aus dem Build übernommen.

- Neu: Eigener Schalter „Dynamischer Tarif: Lastverschiebung aktiv" auf der
  Strategieseite — die Tarif-Lastverschiebung lässt sich jetzt unabhängig von der
  PV-Überschusssteuerung ein-/ausschalten.

- Der Schalter „Temperaturabsenkung aktiv" (vorher „Absenkung aktiv") steht nur
  noch auf der Strategieseite, nicht mehr zusätzlich bei den Thermostat-Einstellungen.

- UI: Seitentitel „Verlauf" (vorher „Verlauf & Prognose").

- UI: jede Seite hat jetzt eine Überschrift mit dem passenden Menü-Icon (vorher nur
  das Dashboard) und eine klarere Gliederung mit Unterüberschriften. „Erzeuger/
  Verbraucher" erscheint nun als Seitentitel statt als schlichte Zeile.

- UI: normale Schreibweise statt durchgängiger Großschreibung. Kategorien (z. B.
  Batterie, Wallbox, Gerätenamen) jetzt rot, fett und etwas größer; Untergruppen
  (z. B. Leistung, Energie) schwarz und fett. Seitenüberschriften etwas größer.
  Versionsnummer nicht mehr im Kopf — sie steht in den Grundeinstellungen.

- **Neu: Peak-Shaving** — Einstellung „Netzbezug deckeln bei (W)" (Grundeinstellungen,
  0 = aus): übersteigt der Netzbezug diesen Wert, entlädt die Batterie gezielt, um
  darunter zu bleiben (höchste Priorität vor Eigenverbrauch/Tarif). Benötigt eine
  Batterie mit Entladeleistungs-Sollwert-Entität.

- Leistungsfluss-Diagramm: auf dem **Handy größere Kreise** mit mehr seitlichem
  Abstand; expandierte Verbraucher (über „+") **stapeln jetzt nach unten** und
  schieben die folgenden Kreise nach unten (Rahmen wächst mit). Im Browser Kreise
  und Schrift minimal größer.

- „Auto angesteckt" (Wallbox) lässt sich jetzt auch mit **Sensor-Entitäten**
  belegen (z. B. ein Coupler-/Plug-Status-Sensor mit Textzustand), nicht nur mit
  Boolean-/Binary-Sensoren — neue, breitere Auswahl-Filtergruppe „connected".
- **Leistungsfluss-Diagramm:** größere, besser lesbare Schrift; auf dem **Handy**
  ein eigenes **vertikales Layout** (Haus oben als Knoten, die übrigen Größen in
  zwei Spalten darunter) statt des breiten, herunterskalierten Stern-Layouts.

- Geregelte Sollwerte werden jetzt **jeden Zyklus (re)geschrieben**, auch
  unverändert — manche Aktoren (z. B. Heizstab-Leistungssollwert) fallen sonst
  auf 0 zurück (Keepalive).
- **Wallbox:** Stopp-Bedingung als **„Fahrzeug-SoC"** ausgewiesen (lädt bis zum
  Ziel-SoC); zusammen mit „Auto angesteckt" sind so beide Fahrzeug-Entitäten
  konfigurierbar.
- **Dashboard:** neue Karte **Batterie-SoC (%)**. Karten **Netz** und **Batterie**
  zeigen nur noch positive Werte mit Richtungstext (**Bezug/Einspeisung** bzw.
  **Ladung/Entladung**).
- **Verlauf:** das Netz erscheint nur noch als **eine** Netto-Linie (Bezug −
  Einspeisung), auch wenn getrennte Bezugs-/Einspeise-Entitäten konfiguriert sind;
  die Batterie ebenso als eine Größe.

- Erzeuger/Verbraucher komplett auf ein **einheitliches Spalten-Layout** umgestellt
  (ein gemeinsames Zeilen-Raster für alle Felder): ausgewählte Entitäten stehen
  jetzt **linksbündig untereinander** in einer Spalte, „Ändern" durchgehend
  **rechtsbündig**, Zahlen-/Eingabefelder sauber ausgerichtet. **Heizkreise**
  übersichtlich als *Name*-Zeile mit *Temperatur*/*Sollwert* darunter (statt
  „Temp … Soll" in einer Zeile). „Steuerung" steht oben im Block; die Entität der
  Steuerung bekommt eine eigene, ausgerichtete Zeile. Eigene, kompakte
  **Handy-Variante** (Label-Zeile, darunter Wert + Aktionen).

- Aufklappbare Boxen (Geräte, Strategien) zeigen jetzt ein **ausgefülltes Dreieck**
  als Aufklapp-Icon: ▶ zugeklappt, dreht zu ▼ beim Öffnen. Die „Steuerung"-Zeile
  unter Erzeuger/Verbraucher ist sauber ausgerichtet; die Entitäts-Auswahl
  (Chip · Ändern · ×) bleibt als Gruppe zusammen und bricht bei Platzmangel
  gemeinsam um.

- UI-Korrekturen: Menü-Schrift etwas kleiner, damit der längste Eintrag in den
  (bei Auswahl gefärbten) Kasten passt; bei Erzeuger/Verbraucher sitzen Checkboxen/
  Radios und ihr Text jetzt **exakt auf einer Höhe** (0 px Versatz).

- UI-Feinschliff: roter **Header bleibt fix** stehen, nur der Inhalt darunter
  scrollt vertikal; **Menü-Texte kleiner** (passen in die Box); Grundeinstellungen
  mit **einheitlich ausgerichteter Eingabespalte**. Im **Handy-Modus**: Hamburger
  und Titel auf gleicher Höhe (Titel etwas weiter rechts), Geräte-Einstellungen
  sauber gestapelt statt verrutscht, und insgesamt **kleinere Schriften/Formen**,
  damit mehr auf die Seite passt.

- **Neues UI-Design** für alle Seiten: einheitliches Layout (festes 1024-px-
  Desktop, schmaler ⇒ horizontaler Scroll; Mobil hochkant mit Hamburger-Menü
  unterhalb des Headers, nur inhaltshoch), saubere Ausrichtung, einheitliche
  Buttons, durchgängiges SVG-Icon-Set. Header wie SmartHub: weiße Logo-Zeile
  über dem roten Banner (neue, dezente Sonne-/Wellen-Grafik), Schrift „Lucida
  Sans". Verbindungs-/Aktualisierungsstatus jetzt klein neben dem Dashboard-
  Titel statt im Header. Einsparung mit Münzen-Icon.

- **Schaltlasten: „unterbrechbar"** – neue Checkbox je geschalteter Entität (in
  der Geräte-Steuerung). An (Default): eine laufende Last darf bei Wegfall von
  Überschuss bzw. günstigem Tarif wieder abgeschaltet werden (z. B. Heizstab).
  Aus: die Last läuft nach dem Start bis zum Ziel durch und wird nur abgeschaltet,
  wenn das Ziel erreicht ist (z. B. Waschmaschine) – gilt für PV-Überschuss- und
  Tarif-Steuerung. Die Strategie-Beschreibungen erwähnen den Status im Beispiel.

- **Strategie-Beschreibungen**: jede Strategie hat jetzt einen **ⓘ-Info-Button**
  (nur Icon) neben ihrem Titel im Strategien-Bereich. Er öffnet eine kompakte
  Beschreibung (Funktionsweise · Voraussetzungen · Beispiel) in einem Overlay
  **innerhalb des SEA-Frames** (kein neues Fenster). Die **Voraussetzungen
  spiegeln die aktuelle Konfiguration** (✓/✗/⚠: PV+Netz, aktivierte Geräte mit
  Priorität, Master-Schalter, Vorrang/Mindest-SoC, Preisquelle + Schwellen,
  Thermostat-Gruppen), und das **Beispiel wird aus den real konfigurierten
  Geräten** und ihren Einstellungen gebaut (Name, Prio, max. Leistung, Stopp-
  Grenze, Deadline „spätester Start“, Lade-/Entlade-Schwellen, Absenk-Delta &
  Vorheiz-Vorlauf). Für PV-Überschuss, dynamischen Tarif und Temperaturabsenkung.

- **Bugfix – PV-surplus no longer runs loads from the battery**: the surplus
  controller regulated the grid to zero (`−grid_w`). With a battery present a
  *discharging* battery holds the grid at ~0 by itself, so a controllable load
  (e.g. an immersion heater) was never throttled back and kept draining the
  battery at night with no PV. The signal now folds in the battery power
  (`surplus_signal()`), so a discharging battery is never counted as surplus.
  New setting **„PV-Überschuss-Vorrang"** (Grundeinstellungen,
  `surplus_loads_first`): *Batterie zuerst* (default) gives loads only the
  export overflow, *Verbraucher zuerst* lets loads take PV directly (battery
  charges with the rest). Either way loads never run from the battery.
  Additional **„Batterie-Mindest-SoC (%)"** (`surplus_battery_min_soc`, 0 = off):
  in *Verbraucher zuerst* the battery keeps its charging power until the SoC
  reaches this reserve, so the storage is filled first before loads may divert
  the charge power.

- Verlauf: **drag a time range with the mouse** on any plot to zoom into it
  (a selection rectangle follows the drag); the range buttons (24 h/7 d/30 d)
  reset. All plots share the same window.
- Menu reordered: Dashboard, Verlauf, Strategien, Einsparung, Geräte,
  Einstellungen. Device gear buttons now open just that device's settings block
  (all others collapsed).
- Verlauf reworked: the **top plot now also shows every device's power**
  (consumers, wallbox, battery …) as toggleable series next to the balance
  (Haus/Netz/Überschuss). For the devices selected (enabled) in the top legend, a
  **set of detail plots** appears below — **one plot per sensor class** (all
  temperatures together, all SoC together, …) from HA's recorder, each auto-scaled
  to its own range. All plots share the **same time window** as the top plot.
  **Cumulative energies (kWh) and the measured device power are not repeated**
  below (the power is already in the top plot) — only additional quantities like
  the forced charge/discharge setpoint appear in the detail plots. New HA history
  fetch (`get_history`) + `/api/history-devices` and `/api/entity-history`.
- **Battery arbitrage – forced discharge at expensive prices**: a new battery
  actuator "Entladeleistungs-Sollwert (erzwungen)" lets the battery discharge on
  the dynamic tariff. New ceiling "Speicher entladen bei ≥ (ct/kWh)" (0 = off):
  when the price is at/above it and the SoC is above the reserve floor, the
  battery is driven to full discharge; charge/discharge/surplus are mutually
  exclusive (one controller). The battery's tariff toggle now governs both
  grid-charge and forced discharge, sharing the reserve-/target-SoC band.
- **Battery grid-charging on the dynamic tariff**: the battery can now charge
  from the grid in cheap/negative price windows. A global price ceiling
  "Speicher netzladen bei ≤ (ct/kWh)" (default **0** = only free/negative) plus a
  per-battery **target SoC band** (min reserve … max). Below the reserve floor it
  tops up at any price; otherwise it charges to the max target only when the
  price is at/under the ceiling (positive-price grid charging loses money to
  round-trip losses, hence the conservative default). The ControlEngine owns the
  battery in both surplus and grid-charge modes (no conflict with the tariff
  engine). Thermal storage already shifts via tariff load-shifting.
- Strategy list tidy-up: the separate "Wallbox PV-Überschussladen" and
  "Batterie-Optimierung" strategies are **removed** — both are already covered by
  the unified surplus list. Renamed: **"PV-Überschuss: Eigenverbrauch und
  Speicherung"** and **"Dynamischer Tarif: Lastverschiebung"**. The per-device
  settings button is now a small **gear icon (⚙)**.
- Tariff load-shifting follow-ups:
  - the **"spätester Start" deadline now also applies to tariff shifting** — a
    deferrable load (e.g. washing machine) is force-started by its deadline even
    if the tariff never got cheap. The deadline shows in the list (⏱ spätestens …).
  - the tariff list gained the **"Einstellungen →"** buttons (right-aligned, as in
    the surplus list).
  - **battery stop is the SoC** (already known) — the device block now asks only
    for the threshold ("Stopp bei SoC ≥ … %"), no entity picker. A threshold of 0
    disables the stop. For other devices the set-stop button is small/icon-only.
- Strategy UI reworked per feedback:
  - **One unified PV-surplus list**: all controllable devices (incl. wallbox AND
    battery) now live in the "PV-Überschuss-Eigenverbrauch" box, **sorted by
    priority** (high → low). The separate Wallbox/Batterie boxes no longer carry
    their own device lists. On the strategy page you only pick devices and set
    priority.
  - **Device-specific settings moved to the device block** under
    "Erzeuger/Verbraucher" (a new "PV-Überschuss-Steuerung" panel per device):
    PV threshold, min runtime/off, max starts, max/min power, W-per-unit, the
    wallbox "vehicle connected" entity and the stop condition.
  - **New "spätester Start" deadline** for deferrable loads (e.g. washing
    machine): if no surplus came by then, the device is **force-started** (from
    the grid if needed) within a 2 h window after the deadline.
- Fix "Aktueller Bezugspreis – (Entität liefert noch keinen Wert)" for dynamic
  tariffs: the current price now reads the entity's live/snapshot value
  immediately (no longer waits for the next hourly state change), the price
  entity is added to the watched set so its value stays fresh, and the value is
  **normalised to ct/kWh** from its unit (EUR/kWh, EUR/MWh, … are converted).
- **Critical persistence fix**: storage was briefly pointed at `/addon_config`,
  which is NOT a mount point — writes went to the container's ephemeral overlay
  and were lost on every restart/update (battery charge setpoint, tariff and
  other settings appeared "not saved"). The `addon_config:rw` share is mounted at
  **`/config`**; storage now uses `/config`. Migration copies any files left in
  `/data` or `/addon_config` over once.
- Fix persistence/refresh of the tariff & strategy settings:
  - the **tariff mode** dropdown now saves immediately (previously the choice was
    lost unless "Speichern" was clicked), and saving the tariff refreshes the
    strategy availability (a dynamic price source enables `tariff_shift` live).
  - the **price-entity picker** is now universal — it matches common dynamic
    tariff sensors (monetary device class, ct/kWh·EUR/kWh·öre/kWh·EUR/MWh… units,
    and number/input_number helpers), so the price entity is actually selectable.
  - added a round-trip persistence test (settings, strategy-loads, wizard config).
- **New strategy – Wallbox PV-Überschussladen (`ev_surplus`)**: the wallbox now
  follows the PV surplus via the unified modulation, with a **minimum charge
  power** (below it the wallbox switches off instead of pulling from the grid)
  and an optional **"vehicle connected"** guard entity. Excluded from the
  generic PV-surplus box (own box), kept selectable for tariff shifting.
- **New strategy – Dynamischer Tarif / Lastverschiebung (`tariff_shift`)**: a new
  `TariffEngine` runs deferrable loads during cheap periods. **Universal price
  model** (`tariff.py`) that adapts to whatever is registered: a dynamic price
  entity's upcoming-price attribute (greedy cheapest fraction), else an absolute
  threshold, else the static HT/NT window. The Strategien page shows the live
  "cheap/expensive" status. Both engines are gated by the master control switch.
- Entity picker: once an entity is selected, suggestions are narrowed to others
  with the **same name prefix** (same leading object-id tokens), so changing a
  pick stays within the same device/integration. Typing a search query lifts the
  restriction to find other families.
- Fix: the battery **charge-power setpoint** picker showed no candidates — its
  field used an undefined `number` unit group, so nothing ever matched. Added
  the `number` unit group (domains `number`/`input_number`); writable number
  entities can now be selected.
- Banner simplified: now shows only the sun (moved slightly right) — the PV
  modules, car and lightning bolt are gone. Gradient unchanged.
- **Persistent settings**: config + history now default to `/config` (the
  host-mapped `addon_config` share) instead of the add-on's private `/data`
  volume, so settings survive an uninstall/reinstall (HA wipes `/data` on
  uninstall). On update, existing legacy files are copied over once
  (non-destructive).
- Strategy device rows: the stop condition is now a small black ■ button on the
  device row; once set it appears on an indented second line. The line no longer
  has a checkbox — to remove it, click "ändern" and pick "— nicht zugeordnet —".
  Tooltips added across the pages (incl. "W/Einheit").
- Savings: the explanation text now adapts to the selected baseline.
- Dashboard: the power-flow frame height fits the diagram and grows for expanded
  sub-power circles (no more cut-off when expanding consumers).

- **Battery control**: the battery can take a charge-power setpoint (new wizard
  field) and then participates in PV-surplus as a **modulating load** (priority
  vs other loads configurable) — the Batterie-Optimierung strategy becomes
  available/active accordingly.
- **Consumer stop conditions**: per device an optional limit (e.g. vehicle SoC or
  temperature ≥ value). When reached the device is "satisfied" — switchable loads
  are switched off, modulating loads driven to 0 — so the **remaining surplus is
  redistributed to the other consumers**.

- Strategies now reference the **configured devices** (from Erzeuger/Verbraucher),
  not raw entities: pick which devices take part in PV-surplus self-consumption
  (and tariff shifting) per device, with priority/threshold/runtime. Multiple
  devices supported.
- **Modulating (regelbare) loads** (e.g. heating rod, wallbox) now absorb the
  **remaining surplus**: the control engine sets their power setpoint (number)
  to drive the grid toward zero, allocated by priority across all modulating
  loads, clamped to a max power, with a W-per-unit factor (e.g. wallbox amps).
  Switchable loads keep on/off control. The engine now uses the signed grid
  signal (fixes shedding after the surplus≥0 clamp).

- Strategien page: each strategy is now an **expandable box** with its settings
  inside (PV-surplus self-consumption holds the master switch + consumer control;
  setback holds its enable + link). Toggling an activation updates the box status
  (verfügbar ↔ aktiv) live.
- Tariff: the dynamic price entity now uses the same **entity picker popup**
  (new 'price' unit group).

- Strategies are now **auto-detected from the configured entities** and listed on
  the Strategien page with status (aktiv / verfügbar / verfügbar (folgt) / nicht
  verfügbar) and what's still missing — PV-surplus self-consumption, wallbox
  surplus charging, dynamic-tariff load shifting, battery optimisation, absence
  setback. `GET /api/strategies`, `strategies.overview()`.
- Removed the obsolete Phase-1 footer.

- Temperaturabsenkung reworked into **groups** (persons + their rooms): a group
  is set back only when **all** its persons are away; comfort returns when anyone
  is home. Multiple groups supported. Optional **predictive pre-heating** to a
  per-group comfort time using a **learned reheat rate** (min/K, EWMA). Frost
  guard + opt-in unchanged. Full-object `POST /api/thermostats` with server-side
  sanitising; live presence shown per group.

- Mobile: the sidebar collapses into a slide-in drawer; a white hamburger icon
  (from SmartHub) appears on the red header to toggle it, with a backdrop and
  auto-close on navigation. Desktop layout unchanged.

- New **Thermostate** settings page (Einstellungen → Thermostate): absence
  temperature setback for room/radiator thermostats (concept 6.6). Add named
  thermostats (climate entity + comfort/eco °C), a global presence entity, frost
  guard and a master switch. A new engine sets eco on away and comfort on home
  (opt-in, respects frost, no-op while presence is unknown). HA `call_service`
  now supports service data (climate.set_temperature).
- Power-flow: wider spacing for expanded sub-power circles (readable labels).

- Merged the forecast into the **Verlauf** view (removed the separate "Prognose"
  menu item): the forecast is drawn as **dashed lines in the same colours** as
  the history; chart colours now match the dashboard. Added **pan ◀/▶** buttons
  and a **free from/to** datetime selection (alternative to 24 h / 7 d / 30 d).
  The history API accepts an explicit `from`/`to` window; a time-based x-axis
  with a "now" marker aligns history and forecast.
- PV surplus is now clamped to **≥ 0** (no negative "surplus" when importing) —
  in the live balance, the recorded history and the forecast.
- History and forecast charts: shared renderer with **nice y-axis ticks, subtle
  horizontal/vertical grid lines and multiple x-axis time labels**.

- Power-flow diagram keeps a **configured device visible even when its entities
  are temporarily unavailable** (shows "–" instead of dropping the node);
  `balance` marks loads `configured` and reports unavailable parts as None.
- Tooltips on the setup buttons (Wählen/Ändern, clear ×, remove −, add, Entfernen,
  expand +/−, close).
- Setup page made compact: each device is now a **collapsible panel** (summary
  shows name + entity count), rows are tight and single-line where possible, and
  named powers/energies/circuits render as inline chips.
- UI cleanup: removed the "Auswahl der Entitäten" view; **Einrichtung** moved
  under **Einstellungen** (Einstellungen → Grundeinstellungen / Einrichtung).
  Tariffs merged into Grundeinstellungen and **pre-filled from the Energy
  dashboard** (price / feed-in / dynamic price entity) when not yet entered.
  Removed the global sign-invert checkboxes (per-instance invert is in the wizard).
- Wizard redesigned to a uniform **instance pattern**: setup starts empty with
  "… hinzufügen" buttons; every genus (PV, battery, heat pump, heating rod,
  wallbox, consumer) is an addable, renameable, removable instance — only the
  grid stays a fixed section. Entity pickers collapse to a summary after
  selection. Heat pump (and any genus) supports **multiple named powers/energies**
  (two pumps + auxiliary heater) and **heating circuits** (temperature + setpoint).
  Controllable instances get a **control radio** (switch via strategy / setpoint)
  that then asks for the actuator entity. Driven by a declarative
  `setup_catalog.INSTANCE_KINDS`; the whole config is posted and **sanitised**
  server-side; the old flat beta config is migrated automatically.
- Power-flow diagram: one **summary circle per device** with a **"+"** that
  expands its individual sub-powers as circles to the right (`balance` now emits
  `loads` with `parts`).
- Energy-dashboard pre-fill now **derives the power (and battery SoC) entity from
  the same HA device** as the configured energy entity — for PV, grid and battery
  — so power slots get pre-selected even when only energy entities are set up.
- Entity selection moved into a **popup dialog** (search + ranked list) opened by
  an "Ändern/Wählen" button, keeping the setup page clean.
- Suggestions now include **diagnostic/config entities** (heat-pump powers are
  often diagnostic) — previously hidden, so search couldn't find them; they rank
  slightly below primary entities. Same for device-based power derivation.
- Wizard: **free, user-named consumers** ("Weitere Verbraucher", e.g. "Allgemein",
  "Wohnungen") — each with its own power/energy entity (multi). They appear as
  extra nodes in the power-flow diagram and as groups in the device view.
- Dashboard: native animated **power-flow diagram** (SVG) — house in the centre
  with PV, grid (import/export direction), battery (charge/discharge + SoC) and
  the configured loads (heat pump, wallbox) as nodes; flow lines animate in the
  direction of energy flow with width scaled by power. Fed by `/api/flow`
  (balance now also reports wallbox power `ev_w`).
- Wizard fixes: multi-select (PV) no longer accumulates silently — already
  selected entities that the current search hides are now pinned (and can be
  unchecked), and the list re-renders after each change. The invert flag
  (grid / battery) is now also visible in the device view, which shows the
  sign-adjusted live power.
- Wizard: added the **wallbox / EV charger** category (charging power + energy +
  control actuators: charge-current setpoint and on/off), prefilled from the
  Energy dashboard's named device consumption.
- Heat pump now supports **multiple entities per quantity**: several power and
  energy meters (e.g. two pumps, summed), plus multi temperature sensors
  (heating circuits, DHW/buffer tanks — shown in the device view) and multiple
  control actuators (climate entities / setpoints per circuit). Energy-dashboard
  prefill accumulates several matching device-consumption entries into a list.

- Guided setup wizard ("Einrichtung"): instead of unreliable auto-classification,
  the user assigns the entities the agent really needs per logical category
  (v1: PV, heat pump, grid), choosing from HA-ranked suggestions — independent of
  which HA device an entity belongs to (solves scattered heat-pump entities and
  power measured by a separate Shelly PM3). New `setup_catalog.py` (slot catalog)
  and `suggest.py` (ranking + energy-prefs prefill); endpoints under `/api/setup/*`.
- Pre-fill from the HA Energy dashboard (`energy/get_prefs`): solar/grid power
  (`stat_rate`), energy meters, price entity and named device consumption
  (heat pump) are proposed top-of-list; one-click "Aus Energy-Dashboard übernehmen".
- The live energy balance now uses this explicit configuration once PV or grid
  power is set (`aggregator.balance_from_config`), falling back to the legacy
  role-based aggregation otherwise.
- Wizard now also covers the **battery** (charge/discharge power + SoC, sign
  flag; included in the balance: house = pv + grid − battery) and **control
  actuators** — for the heat pump a climate/thermostat entity or a number
  setpoint for the temperature setback/raise (marked "Stellgröße").
- Device view reworked: entities grouped by logical category (PV / battery /
  heat pump / grid) with live values, regardless of their HA device
  (`GET /api/categories`).
- App version shown in the header.
- Tests for the suggestion ranking, energy-prefs prefill (incl. battery) and
  config-based balance (incl. battery).

- Phase 2 (forecast): history-based household consumption forecast
  (recency-weighted hour-of-day load profile, weekday/weekend split) with a
  backtest of forecast accuracy (MAE/MAPE).
- Phase 2 (forecast): PV forecast from Home Assistant. Primary source is the
  Energy-dashboard `energy/solar_forecast` (Forecast.Solar; HA-cached, no
  upstream API hit, no entity config needed); a configurable PV-forecast entity
  (Solcast `detailedForecast`, generic list) is the fallback. The solar forecast
  is pulled on connect and refreshed every 15 min. Plus the resulting PV-surplus
  forecast (surplus = PV − load) — all via `GET /api/forecast`.
- New "Prognose" dashboard view: 24 h consumption/PV/surplus chart, kWh summary
  and forecast-error figure.
- Add unit-test setup (`pytest`, `requirements-dev.txt`) covering the forecast.

## 0.1.0

- First version: detection and curation of energy-related entities, live energy
  flow, history, consumer configuration, tariffs (purchase price/feed-in
  compensation), savings calculation with selectable baseline, and
  (off-by-default) PV-surplus control.
