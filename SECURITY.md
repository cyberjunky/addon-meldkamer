# Security Policy

## Supported versions

This is a personal project under active development. Only the latest released
version receives fixes. Please update to the newest version before reporting a
problem.

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue:

1. Go to the repository's **Security** tab.
2. Choose **Report a vulnerability** (GitHub private vulnerability reporting).

Include what you found, how to reproduce it, and the add-on version. You will
get an acknowledgement as soon as possible. Please give a reasonable amount of
time for a fix before any public disclosure.

## What this add-on handles

So you know what is at stake:

- **Received radio messages.** The add-on decodes public P2000 FLEX broadcasts
  from an RTL-SDR/HackRF or a network stream. Decoded messages can contain
  personal data (names, addresses) and are stored in a local SQLite database in
  the add-on's data directory. Nothing is uploaded to any third party.
- **The dashboard** is served through Home Assistant ingress, so it is reachable
  only to users already authenticated to Home Assistant.
- **MQTT credentials** (when you enable MQTT) are read from the add-on options and
  used only to connect to the broker you configure.
- **Locations** are looked up at the Dutch PDOK Locatieserver (streets, towns,
  postcodes) and, only when you configure an OpenCage key, at OpenCage. Only the
  address text is sent, never the message itself.
- **No telemetry.** The add-on makes no analytics or tracking calls.

When reporting or attaching logs, redact anything that identifies real people or
locations, and any credentials.
