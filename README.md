# MQTT IoT Edge-Cloud

A practical IoT demo that connects a **real Raspberry Pi 4** to a VPS with MQTT. The Pi runs a small local device page; the VPS runs an authenticated Streamlit control panel, an Eclipse Mosquitto broker, and a Django-based MQTT ingestion/data layer.

## What the project does

- Send text messages from the Raspberry Pi to the cloud dashboard.
- Publish sample temperature and humidity telemetry.
- View device connectivity, message history, acknowledgements, and sensor charts.
- Send output on/off commands from the cloud panel to the Raspberry Pi.
- Send a text message or ping command from the cloud panel to the device.
- Optionally control a low-voltage LED connected to Raspberry Pi BCM GPIO17.

The temperature and humidity values are generated in software for the classroom demonstration. A physical sensor is not required. The output is simulated by default.

## Architecture

```text
Local browser
    |
    | HTTP :8080
    v
Raspberry Pi 4
  - Flask local page
  - Paho MQTT device client
    |
    | MQTT over WSS / TLS
    | server.echonode.website:443/mqtt
    v
VPS / Docker network
  - Traefik or Caddy reverse proxy
  - Streamlit Control Panel :8501
  - Django ORM + SQLite data store
  - Django MQTT Listener
  - Eclipse Mosquitto
       - private TCP :1883 (panel / listener)
       - WebSockets :9001 (reverse proxy only)
```

## Server panel

The Streamlit panel is served at **https://server.echonode.website**. It includes:

- **Overview:** Pi connectivity, message counters, Broker status, telemetry charts, and latest messages.
- **Device control:** output on/off commands, cloud-to-device messages, and ping requests.
- **Telemetry:** temperature/humidity charts, date-range selection, statistics, and CSV export.
- **Message explorer:** search and filter events, telemetry, and command acknowledgements; inspect full JSON payloads and export results.
- **Broker and topics:** private Broker connection check, available Mosquitto $SYS counters, and the device topic map.
- **System health:** database/Broker/device state and non-secret runtime configuration.

The panel has its own login controlled by `PANEL_USERNAME` and `PANEL_PASSWORD` in the VPS `.env` file. These are separate from the Raspberry Pi's local web password.

Django remains responsible for the data models, database migrations, and background MQTT ingestion. The old Django HTML dashboard is no longer used.

## MQTT topic layout

The default topic prefix is `iotlab`; the example device ID is `raspberry-pi-01`.

| Topic | Direction | Purpose |
|---|---|---|
| `iotlab/devices/raspberry-pi-01/events` | Pi → VPS | Text messages and device events |
| `iotlab/devices/raspberry-pi-01/telemetry` | Pi → VPS | Temperature and humidity samples |
| `iotlab/devices/raspberry-pi-01/status` | Pi → VPS | Retained online/offline state |
| `iotlab/devices/raspberry-pi-01/command` | VPS → Pi | Output, text-message, and ping commands |
| `iotlab/devices/raspberry-pi-01/ack` | Pi → VPS | Command acknowledgements |

The application uses QoS 1 for normal events, telemetry, commands, and acknowledgements. Mosquitto ACLs restrict each identity to the topics it needs. The example MQTT usernames are referenced by `mosquitto/aclfile`; if you change either username in `.env`, update the matching ACL section as well.

## Requirements

### VPS

- Linux server with Docker Engine and the Docker Compose plugin.
- DNS A record for `server.echonode.website` pointing to the VPS.
- One of the supported reverse-proxy modes:
  - `traefik-swarm`: an existing Traefik deployment using a Docker Swarm overlay network (default).
  - `traefik-compose`: an existing Traefik Docker provider and shared Docker network.
  - `standalone`: bundled Caddy; ports 80 and 443 must be available.

### Raspberry Pi

- Raspberry Pi 4 running Raspberry Pi OS or another Debian-based Linux.
- Network access to `server.echonode.website` on TCP 443.
- systemd (used by the installation script).

## Deploy the VPS

1. Clone this repository and enter the project directory.
2. Create the environment file:

   ```bash
   cp .env.example .env
   ```

3. Edit `.env`. Set strong unique values for:
   - `DJANGO_SECRET_KEY`
   - `PANEL_USERNAME` and `PANEL_PASSWORD`
   - `MQTT_DEVICE_PASSWORD`
   - `MQTT_SERVER_PASSWORD`
4. Set `PROXY_MODE`, and make sure Traefik's network, entrypoint, and certificate resolver match the existing VPS configuration.
5. Deploy:

   ```bash
   chmod +x scripts/*.sh
   ./scripts/deploy_vps.sh
   ```

6. Open **https://server.echonode.website** and sign in using the Streamlit panel credentials from `.env`.

The deployment creates the Mosquitto password database in a persistent Docker volume. The Streamlit service runs Django migrations and registers the Raspberry Pi demo device before it becomes healthy. The deployment script starts the MQTT listener after the panel is healthy.

## Install the Raspberry Pi client

1. Copy or clone the repository onto the Raspberry Pi.
2. From the repository root, run:

   ```bash
   chmod +x scripts/install_pi.sh
   ./scripts/install_pi.sh
   ```

3. Edit the generated environment file:

   ```bash
   sudo nano /etc/mqtt-iot-pi.env
   ```

4. Set `MQTT_PASSWORD` to the same value as `MQTT_DEVICE_PASSWORD` on the VPS. Set unique values for `LOCAL_WEB_PASSWORD` and `FLASK_SECRET_KEY`.
5. Start the service:

   ```bash
   sudo systemctl start mqtt-iot-pi
   sudo systemctl status mqtt-iot-pi
   ```

6. Get the Pi's LAN address and open the local page from a browser on the same network:

   ```bash
   hostname -I
   ```

   Browse to `http://<RASPBERRY_PI_IP>:8080/` and sign in using `LOCAL_WEB_PASSWORD`.

## Try the demo

1. Open the Raspberry Pi local page and the VPS Streamlit panel in separate browser tabs.
2. Send a text message from the Pi page; it should appear in the panel's message explorer.
3. Publish sample temperature/humidity data and view it in the telemetry charts.
4. From the Streamlit panel, send an output on/off command or a text message to the Pi.
5. Check the acknowledgement in the message explorer.
6. Optionally connect a low-voltage LED and suitable current-limiting resistor to BCM GPIO17 and GND, then set `ENABLE_GPIO=1` in `/etc/mqtt-iot-pi.env`. Never connect mains voltage or a high-power load directly to a GPIO pin.

## MQTT transport and security

- Raspberry Pi external connection: MQTT over secure WebSockets (WSS) on port 443 at `/mqtt`.
- Streamlit panel and Django listener: private MQTT/TCP on port 1883 inside the Docker network.
- Mosquitto WebSocket listener: port 9001, reachable from the public side only through the HTTPS reverse proxy.
- Raw MQTT port 1883 is not published to the VPS host.
- Separate MQTT credentials and topic ACLs are used for the device and server.
- The local Flask page is intended for a trusted LAN. Do not expose port 8080 directly to the public Internet.

## Useful operations

```bash
# Docker Compose
docker compose logs -f web listener broker

# Docker Swarm
docker service logs -f mqtt-iot_web
docker service logs -f mqtt-iot_listener
docker service logs -f mqtt-iot_broker

# Raspberry Pi
sudo journalctl -u mqtt-iot-pi -f
sudo systemctl restart mqtt-iot-pi
```

## Project layout

```text
.
├── dashboard/                 # Django models, migrations, MQTT listener
├── iotlab/                    # Django settings used by the data layer
├── server_panel/              # Streamlit authentication, data access and UI
├── pi_device/                 # Raspberry Pi Flask page and MQTT client
├── mosquitto/                 # Broker configuration and topic ACL
├── scripts/                   # VPS deployment and Pi installation scripts
├── deploy/                    # Raspberry Pi systemd unit
├── streamlit_app.py           # Streamlit entry point
├── Dockerfile
├── docker-compose.yml
└── docker-stack.traefik-swarm.yml
```

## Tests

Run the Django model tests in an environment with the dependencies installed:

```bash
python manage.py test
```

A live test between the deployed VPS and physical Raspberry Pi is still required to verify DNS, TLS, proxy routing, MQTT credentials, and GPIO hardware.
