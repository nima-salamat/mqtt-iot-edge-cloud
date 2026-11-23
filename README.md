# MQTT IoT Edge-Cloud

A practical MQTT demonstration that connects a **real Raspberry Pi 4** to a separate VPS-hosted Django dashboard through an authenticated Mosquitto broker.

> **Source bundle:** The complete source tree, Persian setup guide, presentation plan, demo script, and SVG diagrams are included in [`dist/mqtt-iot-edge-cloud-source.zip`](dist/mqtt-iot-edge-cloud-source.zip). Extract the archive to work with the project files.

## Architecture

- **Raspberry Pi 4:** runs a local Flask control page on the LAN (`http://<pi-ip>:8080`). It publishes messages and sample telemetry, subscribes to device commands, and returns acknowledgements.
- **VPS:** runs Django, a background MQTT listener, and Eclipse Mosquitto.
- **Transport:** MQTT over secure WebSockets (WSS) on `server.echonode.website:443`, using the `/mqtt` path.
- **Reverse proxy:** supports an existing Traefik Swarm deployment, Traefik with Docker provider, or standalone Caddy.
- **Security:** separate MQTT identities for the Pi and Django service, topic ACLs, TLS at the public edge, authenticated local UI, CSRF protection, and no public raw MQTT port.

## Message flow

1. The Pi publishes text events or sample telemetry to its device-specific topics.
2. Mosquitto delivers those messages to the Django listener.
3. Django stores received events and telemetry for the web dashboard.
4. An authenticated dashboard user publishes an `on` or `off` command.
5. The Pi receives the command and publishes an acknowledgement.

The sample temperature/humidity values are software-generated for the classroom demo; no physical sensor is required. The output is simulated by default. An optional LED can be connected to BCM GPIO17 with a suitable resistor after enabling GPIO in the device environment file.

## Deployment target

The default hostname is **https://server.echonode.website**. DNS and VPS deployment are not performed automatically by this repository. Configure the DNS A record, proxy network, entrypoint, certificate resolver, and secrets on your own VPS before deploying.

## Quick start

1. Download and extract `dist/mqtt-iot-edge-cloud-source.zip`.
2. Follow `README.fa.md` for the full Persian deployment guide.
3. Configure the VPS `.env` from `.env.example` and deploy using `scripts/deploy_vps.sh`.
4. Install the Raspberry Pi service using `scripts/install_pi.sh` and configure `/etc/mqtt-iot-pi.env`.
5. Open the Pi's local page and the VPS dashboard to demonstrate two-way MQTT messaging.

## Repository contents

- `dist/mqtt-iot-edge-cloud-source.zip` — complete source bundle (Django dashboard, Pi Flask app, Mosquitto, Docker/Swarm manifests, scripts, docs, SVG diagrams).
- `README.fa.md` — Persian project and deployment guide (inside the source bundle).
- `docs/` — scientific presentation plan, speaker/demo guide, and references (inside the source bundle).

## Important security notes

- Never commit `.env`, `/etc/mqtt-iot-pi.env`, generated Mosquitto password files, database files, or private keys.
- Keep ports `1883` and `9001` private; expose only HTTPS through the configured reverse proxy.
- Replace all `REPLACE_*` placeholders with unique, strong secrets before deployment.
- The local Flask server is intended for a trusted LAN classroom demo. Do not expose port `8080` directly to the public Internet.

## Validation status

The repository package is statically checked for Python syntax, shell syntax, YAML parsing, and archive integrity. A live VPS deployment and end-to-end hardware/network test must still be performed in the target environment.
