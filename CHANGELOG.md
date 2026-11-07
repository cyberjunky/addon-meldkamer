# Changelog

## Unreleased

- Project tooling, no functional change: the Python dependencies are listed in `requirements.txt` so Dependabot keeps them and the base image and workflow actions current; a lint workflow checks the code on every push and pull request; a security policy (`SECURITY.md`) says how to report a vulnerability.
- Fixed: importing several provinces with Import BAG could stop after a few. The list of provinces was kept by the dashboard page, so one failed progress request (Home Assistant renewing its session, a short network drop, a tab in the background) or leaving the page ended the run while the add-on was still busy. The add-on now runs the whole list itself; the page only shows the progress ("3/12 Gelderland 45%"), keeps trying when a request fails, and picks a running import up again after a reload. A province that fails is reported by name and the others still run.

## 1.4.4

**After updating:** nothing to do.

- A street found by its ending keeps dotted initials in front of it: "J.P. Coenstraat", "H.J.E. Wenckebachweg".
- Streets and towns with accents in their name are recognised: P2000 messages are sent without accents, so "Belgielaan" now finds "Belgiëlaan", "Burgemeester Roellstraat" finds "Burgemeester Röellstraat" and "Loenga" finds "Loënga". Over 3,500 BAG streets have an accent. The street keeps its official spelling.
- A street found by its ending (-straat, -weg, -laan, …) when it isn't in the Streets table no longer takes in the codes and words before it: "P 1 BNH-01 Woningbrand Kerkstraat" gives "Kerkstraat" instead of "1 BNH-01 Woningbrand Kerkstraat", and "A2 AMBU 17142 2e Middellandstraat" gives "2e Middellandstraat".
- After a BAG import of several provinces the message says "new places" and "new streets", like the status of one province; Import All Places ends in phase "Complete"; and the Locatieserver paging limit is no longer logged as a warning when the postcodes are fetched per area anyway.
- The Sensors description on the Configuration tab mentions `incident_types` and its values.

## 1.4.3

**After updating:** nothing to do. New abbreviations and spoken-text rules are added on the first start; your own edits are kept.

- Precise positions from a full postcode: a message with "3068HA" is placed at that postcode's centre (a few houses), looked up once at the free PDOK Locatieserver and kept in the geocode cache. Comes before the street position and OpenCage; shown as "Postcode" in the detail and counted for the distance from home.
- Incident type recognised from the text (brand, automatische melding, verkeersongeval, medisch, waterongeval, gas/stank, liftopsluiting, storm, dier, hulpverlening, GRIP): shown in the detail, filterable with chips on the Console, usable in sensors with `incident_types`, and in the event and MQTT attributes as `incident_type`/`incident_label`. Older messages get it too.
- Reference numbers (rit, bon, icnum), the meldkamer talk group ("BAD-02") and "Directe inzet: ja" are separate fields (`ride_number`, `bon_number`, `incident_number`, `talk_group`, `direct_deployment`), shown in the detail. A vehicle named with its type ("ambu 16-107") supplies the region when the capcodes don't.
- Spoken text: "hind." and "ass." are read in full, as are ivm, tbv, evt, VKO, OMS, BMI, AED, TS, HW and HV; road numbers are spoken as "N 209" / "A 27".
- Fixed: abbreviation tags matched ordinary lowercase words. "bon 170616" was tagged as Brandweer Oost-Nederland, "rit:" as Rampen identificatie team, "me" as Mobiele Eenheid, "ab" as Adembescherming. A code now only matches when written as the code itself ("OvD") or in capitals.
- New abbreviations: AMB, MKA, MKB, MKP (meldkamers), VKO (verkeersongeval), BMI (brandmeldinstallatie), AED, RV (redvoertuig), ZGB (zeer grote brand), ALS. Added to existing installs on the next start.
- Ambulance numbers written in lower case ("ambu 16-107") are recognised as vehicles too.

## 1.4.2

**After updating:** nothing to do. Browser notifications need Home Assistant over HTTPS; on a plain http:// address the browser refuses them without asking.

- Fixed: the Console's "Reference data not imported yet" hint stayed visible, with empty brackets, once everything was imported.
- Town shortnames in a message ("SGRAVH", "ROTTDM") are shown as a tag in the message detail with the town they stand for, like the other abbreviations. The message text itself stays as received.
- The map library (Leaflet) is bundled with the add-on instead of loaded from the unpkg CDN. Browsers with strict tracking protection (Edge, Brave) blocked that download and showed no map; the dashboard now needs no internet access for its own interface.
- The 💬 browser-notification toggle now says what to do when the browser blocked notifications (lock icon → Notifications → Allow) or hid the permission prompt behind the bell icon in the address bar, instead of one "blocked" message for both, and that notifications need Home Assistant over HTTPS (a plain http:// LAN address is refused by the browser without a prompt).

## 1.4.1

**After updating:** run **Import BAG** again for your provinces (Data → Streets): it completes the postcodes of Amsterdam, Rotterdam and The Hague and merges streets that BAG listed in two spellings.

- Import BAG: the postcodes of Amsterdam, Rotterdam and The Hague are complete now. The Locatieserver stops at 10,000 results, which those three exceed, so their postcodes are fetched per postcode area; before, part of their streets got no postcode. A postcode list that is still incomplete now shows as a warning in the import result (#47).
- The merge of streets that BAG listed in two spellings no longer runs at every start of the add-on; a province's streets are merged when you import that province again.

## 1.4.0

**After updating:** nothing to re-import. The first start merges streets that BAG listed in two spellings (a second or so with all provinces imported); the Settings tab is now called **Data**; browser notifications need Home Assistant on HTTPS.

- The message detail shows which of your sensors the message matched.
- Links to a message: selecting one puts `#msg=<id>` in the URL, and opening the dashboard with such a hash shows that message, also when it has left the live list. The `meldkamer_message` event has a `dashboard_url` for this, usable as the `url` of a notification.
- Keyboard: `j`/`k` or the arrow keys step through the messages, `1`/`2`/`3` switch between Console, Wallboard and Lanes, Escape closes the floating detail.
- A hint on the Console while the capcodes, places or streets are not imported yet, with a button to the Data tab.
- The map shows your home (from Home Assistant's home zone).
- New `announce.quiet_hours` option (e.g. `23:00-07:00`): no speaker announcements between those times, and the dashboard's sound, speech and notifications stay silent too (the toggles dim and say so).
- Info tab: messages per hour, per discipline and the busiest towns of the last 24 hours; a Sensors overview with each sensor's filters, zone radius, announce setting, matches since start and last match; the geocoding rows moved from Device to their own Locations card.
- The Settings tab is now called **Data**: it holds the imports and tables, and the add-on's options live on its Configuration tab in Home Assistant, which the tab now says. "Reset database" moved from the toolbar into a Danger zone at the bottom.
- Backup and restore of the whole database on the Data tab: download one `.sqlite3` file (a consistent snapshot, the add-on keeps running) or restore such a file; the file is checked before the database is replaced.
- Browser notifications (💬 toggle in the top bar): new messages of the current feed notify while the dashboard is in the background; clicking a notification opens the message.
- Console: a search box (press `/`) searches all stored messages, not just the live list; P1/P2/P3+ and discipline chips narrow the list; "Older messages" at the bottom loads earlier messages from the database. Previous/Next and the keyboard walk the list as shown.
- Top and bottom bar no longer show the same things twice. The top bar keeps MELDKAMER P2000 FLEX, the views, the sensor filter, the toggles and the clock; the receiver status and the MSG/h, FRAMES/min and INCIDENTS counters moved to the bottom bar. The receiver status and the live connection are one status there: the receiver's state while connected, "Connecting…" or "Reconnecting…" when the dashboard has no live connection.
- Fixed: a street was looked up in every town in the country, not just the town in the message. Messages got the postcode of a street of the same name elsewhere ("Kerkstraat Uitgeest" got the postcode of the Kerkstraat in Amsterdam), or a street named after an everyday word ("Inzet", "Dienst", "Contact") put a message in a town it never mentioned. A street now only matches in the message's town; without a town only clear street names (several words, or ending in -straat, -weg, -laan and so on) are matched.
- Streets that BAG lists in two spellings for one town ("De Akkers" and "de Akkers") are imported once, and existing ones are merged once, the first time 1.4.0 starts: the one with a position is kept and takes over the other's postcode.
- Import BAG no longer gives "Kapel Avezaath" the streets of "Kapel-Avezaath" (and the other way round): only streets of the exact town are imported.
- The Import BAG status now ends in phase "Complete" and says "new places" and "new streets", as only new ones are counted.

## 1.3.0

### Streets import
- Import BAG can import several provinces in one go: tick the provinces you want (or Select all / Deselect all) and they are imported one after the other, with a summary at the end (#42, thanks @SvenKortekaas).
- The Streets card shows which provinces you have imported, and the Import BAG dialog marks them with their street count. A ⚠ marks a province whose streets have no positions yet (imported before 1.2.9): run Import BAG for it again. Worked out from the streets in the database, so it stays right after a CSV import or clearing the table.
- Fixed: Import BAG skipped all streets of a place when one of its streets only appeared in the postcode results (warning "Failed to import streets for <place>: <street name>"). Those streets are now imported without a position (#41, thanks @SvenKortekaas).

### Other
- Dutch translations for the add-on configuration: option names and descriptions show in Dutch when Home Assistant's language is Dutch. The `mqtt` option now has a name and description in both languages.
- The dashboard showed the wrong add-on version (1.2.9 showed v1.2.8); it now reads the version from the add-on itself.

## 1.2.9

**After updating**, in Settings:
1. Places → **Import All Places**, to give every town its centre position.
2. Streets → **Import BAG** for each province you imported before, to add street positions. Your streets and edits are kept.
3. Texts → **Import Texts** (this resets built-in texts you edited yourself; or just edit `B1` and `B2`), and in TTS Replacements change the `B1`/`B2` rules to "Ambulance besteld vervoer" (or delete them and restart the add-on), to get the corrected B1/B2 meaning below.

### Locations
- Street positions from the BAG import: the Streets import now also stores each street's position (its centre, from PDOK), and messages with a known street are placed on it without an OpenCage request. OpenCage is only used for streets that aren't in the table, so street-level map pins work without an OpenCage account too (#26).
- Places get their position (the town's centre, from PDOK) from "Import All Places" and "Import BAG". Before, both imports left places without coordinates, so a message without a street (or with a street that couldn't be placed) only got a map pin when you had entered the place's position yourself.
- Streets have `latitude` and `longitude`: shown on the Streets page, editable in the add/edit form, and in CSV export and import (optional columns; an import with coordinates also fills them in for streets you already have). Coordinates outside the Netherlands are rejected.
- New `geocoding.only_sensor_matches` option (default off): only call OpenCage for messages that match a sensor, so a receiver hearing the whole country doesn't use up the free tier on messages you don't follow. Other messages use cached coordinates or the town centre. Zone filters also count messages up to 2 km outside the zone, as the street may lie inside it. The Info tab shows how many messages skipped OpenCage (#27).
- "km from home" is now also left out for a message with a street that couldn't be placed (it was put at the town centre).

### Fixes
- BAG import for Friesland failed with "No woonplaatsen found": PDOK names the province "Fryslân" (#29).
- B1/B2 mean ambulance "besteld vervoer" (planned transport), not fire service with/without urgency, in the abbreviation explanation and the spoken text (#28, thanks @SvenKortekaas). Existing installs keep their old texts until updated, see above.

### Dashboard
- Phone layout: on screens up to 700 px wide the top bar wraps into two rows (view tabs; receiver status, messages per hour, sensor filter and the sound/speech/map/theme toggles), so the sensor filter and toggles are no longer pushed off-screen. Settings and Info show as icons; the clock is hidden (#25).

### Documentation
- README: "Getting the most out of it" (which imports to run and what each one enables) and "How it works" (decoding, parsing, the location lookup order, incidents and output).

## 1.2.8

- "km from home" is only shown for messages with a street. A message naming just a city is placed at the city centre, so its distance was that of the centre (e.g. "3.9 km from home" for any Amsterdam message). Also for the `distance_km` attribute, and for messages already in the history.

## 1.2.7

- Support and Donate cards on the Info tab: documentation, reporting a problem, the version and changelog, and donating via PayPal or GitHub Sponsors.
- Removed the ppm calibration from the Settings tab. Set `ppm_correction` (and `backup_ppm_correction`) in the add-on configuration; the Info tab's reception quality shows whether a value decodes well.

## 1.2.6

- New **Vehicles** table (Settings → Database): what each vehicle number is (type and description). A vehicle only gets a type and photo when it's in this table or the message names its type ("TS 07-1782"); the type is no longer derived from the number's digits (1.2.5 did that, and regions don't apply the numbering plan consistently). Filled automatically from the capcode list (descriptions such as "TS 01-2632" or "Ambulance-07-101") and from messages that name a vehicle with its type; add, edit, export or import (CSV) your own.
- Vehicle numbers are now found in fire brigade messages: the alerted units at the end (e.g. "… Scheemda 013693 012933" → 01-3693, 01-2933). The message detail lists all units; the first one known in the Vehicles table is shown with its type and photo.
- Messages from history show their vehicles too (they weren't stored, so they only showed on live messages); this works for all existing messages.

## 1.2.5

- Real photos for vehicles without an uploaded photo: tankautospuit, autoladder, hulpverlening, haakarm, schuimblus, waterongevallen, officier van dienst, dienstbus (also used for gevaarlijke stoffen units), ambulance, traumahelikopter, KNRM reddingboot and politie. Freely licensed photos from Wikimedia Commons, cut out (transparent background) with a "MELDKAMER" licence plate, credited under the photo (see `src/vehicle_images/CREDITS.md`).
- Fire brigade numbers pick the photo from their type digit (Nationaal Nummerplan Brandweer: RR-KKTV, e.g. 11-0293 is type 9, an OvD car), so the right vehicle shows even when the message's abbreviation says otherwise.
- Your own uploaded photos (per vehicle number or category) still come first.

## 1.2.4

- The map keeps your zoom level: focusing a new incident moves the map without zooming in or out, and the last map position and zoom are restored after a reload.

## 1.2.3

- The sensor filter uses exactly the same style as the view tabs.
- Tab and filter labels are centred exactly.
- The sound, speech, auto-focus and theme buttons form one group in the tab style; enabled ones are highlighted.
- On screens up to 1400 px wide the sensor filter is a dropdown instead of a row of buttons.
- The live feed no longer freezes when the connection is silently dropped (e.g. overnight): the add-on sends a heartbeat and the dashboard reconnects by itself when it stops hearing from it. A rejected reconnect (shown as a lasting "Reconnecting…") is retried after 5 seconds.
- Loading the message list retries before showing "Failed to load messages", which now includes the reason.
- Fits smaller screens (1280–1400 px wide, up to 800 px high): compact top bar, slimmer Console side column, shorter Wallboard ticker.

## 1.2.2

- The sensor filter in the top bar is a labelled, outlined control like the view tabs, so it stands out in every view.
- Bigger Wallboard ticker (taller, 15 px message text over up to three lines) and a bigger clock.
- Removed the unused `/config` and `/share` folder mappings (Supervisor warned about the deprecated `config` mapping); `/ssl` stays for MQTT TLS certificates.

## 1.2.1

- Tabs: **Settings** (was Database; now also holds the ppm calibration) and **Info** (was Advanced), in that order after the views.
- Message detail always shows the vehicle photo slot, the date and distance from home, an "escalated" badge on the incident, the raw message, and Previous/Next to step through messages.
- Wallboard and Lanes show the detail floating in the map's corner instead of a centred popup; dialogs no longer dim the page.
- Draggable dividers between the panes of Console, Wallboard and Lanes (double-click resets).
- Console's map and detail column grows with the screen; lanes are capped so the map gets the rest.
- Messages with an A/B urgency code that also reach fire brigade capcodes go in the Ambulance lane.
- Requires Home Assistant 2026.9 or newer (map tiles always come through its map tile proxy).
- Fix: the built-in abbreviation dictionary is seeded on startup again.
- Settings and Info pages use the dashboard's look: flat panels, compact buttons, mono values, no emoji headings, and larger, more readable text.
- New screenshots.

## 1.2.0

- New compact dashboard with three views, switched with tabs in the top bar:
  - **Console**: a dense message table, with the map and the full message detail (vehicle photo, incident timeline, earlier messages at the address) docked on the right.
  - **Wallboard**: a large map with live counts, incidents sorted by priority, and a ticker of the newest messages.
  - **Lanes**: one column each for Brandweer, Ambulance, Politie and other disciplines, next to the map.
- Wallboard and Lanes open the message detail in a popup; Console shows it docked.
- The theme follows Home Assistant's light/dark mode, with a button to force light or dark.
- Fits one screen: receiver status, messages per hour, frames per minute and open incidents in the top bar; decoder and device status in a status bar.

## 1.1.1

- Prebuilt images: the add-on is now pulled from `ghcr.io/cyberjunky/addon-meldkamer` instead of being compiled on your system, so installing and updating take a minute instead of a long build.
- Based on Alpine 3.23 (Home Assistant's multi-arch base image).
- Only 64-bit systems (`amd64`, `aarch64`) are supported, the architectures Home Assistant's image builder supports.

## 1.1.0

### Breaking changes

- Sensor location filters now use Home Assistant zones: `center_lat`, `center_lon` and `radius_km` are replaced by `zone` (e.g. `zone: zone.home`), which supplies both the centre and the radius. Create a zone in Home Assistant for any other area you filtered on.
- The `distance` message attribute is replaced by `distance_km` (distance from `zone.home`). Sensor attributes still carry `distance`, now the distance to the centre of that sensor's zone.
- Sensor states are the spoken (TTS) text of the message; `body` holds the original.

### New

- `meldkamer_message` event for every message and `meldkamer_incident_escalated` when an incident escalates (`ha_events`).
- Receiver health sensors: `binary_sensor.meldkamer_receiver_online` and `sensor.meldkamer_messages_per_hour` (REST and MQTT).
- Announce messages on Home Assistant speakers with `tts.speak` (`announce` options, `announce: true` per sensor).
- Messages to the same address are grouped into incidents on the dashboard (`incident_window_minutes`).
- Repeat suppression for messages the pager network resends (`repeat_window_seconds`).
- Map history heatmap with a time slider.
- Reception quality on the Advanced tab: FLEX frames per minute, corrected bit errors, failed decodes, lost syncs.
- Automatic ppm calibration on the Advanced tab, saved to the add-on configuration.
- Bias-tee option for RTL-SDR (`receiver.bias_tee`).
- Failover to a backup `rtl_tcp` receiver (`receiver.backup_host`), and an automatic restart when the receiver stops producing frames (`receiver.offline_after_minutes`).
- README example for Home Assistant's Map card.

### Changed

- Browser speech, sensors, MQTT and speakers all use the same server-side TTS text.
- Removed unused POCSAG parsing and environment-variable configuration.
