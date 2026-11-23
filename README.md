# MQTT IoT Edge-Cloud

A small end-to-end Internet of Things demo connecting a **real Raspberry Pi 4** to a separate VPS through MQTT. The Raspberry Pi hosts its own local control page, while the VPS runs a Django dashboard and an Eclipse Mosquitto MQTT broker.

The project demonstrates device-to-cloud telemetry, cloud-to-device commands, MQTT publish/subscribe topics, QoS 1 delivery, and command acknowledgements.

## Architecture

```text
Browser on the local network
           |
           | HTTP :8080
           v
+-----------------------------+
| Raspberry Pi 4              |
| Flask local control page    |
| Paho MQTT device client     |
+-----------------------------+
           |
           | MQTT over WSS / TLS
           | server.echonode.website:443/mqtt
           v
+-----------------------------+
| VPS                         |
| Reverse proxy (Traefik/Caddy)|
|       |                     |
|       +--> Django dashboard |
|       |                     |
|       +--> Mosquitto broker  |
|                |            |
|          Django MQTT listener|
+-----------------------------+
```

### Components

- **Raspberry Pi application (`pi_device/`)**: Flask web interface for sending text messages, publishing sample temperature/humidity telemetry, viewing connection state, and receiving commands. The device client runs as a systemd service.
- **Cloud application (`dashboard/`, `iotlab/`)**: Django dashboard with login, device/message history, telemetry storage, and an authenticated endpoint for issuing device commands.
- **MQTT broker (`mosquitto/`)**: Eclipse Mosquitto with WebSocket transport, username/password authentication, persistent storage, and topic-level ACLs.
- **Deployment files**: Docker Compose, Docker Swarm, Traefik, Caddy, and Raspberry Pi installation configuration.

The temperature and humidity values are generated in software for the demonstration. No physical sensor is required. The output is simulated by default; an optional LED can be connected to BCM GPIO17 with a suitable resistor.

## MQTT topic layout

The default topic prefix is `iotlab` and the example device ID is `raspberry-pi-01`.

| Topic | Direction | Purpose |
|---|---|---|
| `iotlab/devices/raspberry-pi-01/events` | Pi → VPS | Text messages and device events |
| `iotlab/devices/raspberry-pi-01/telemetry` | Pi → VPS | Sample temperature and humidity |
| `iotlab/devices/raspberry-pi-01/status` | Pi → VPS | Device online/offline state |
| `iotlab/devices/raspberry-pi-01/command` | VPS → Pi | Cloud commands such as output on/off |
| `iotlab/devices/raspberry-pi-01/ack` | Pi → VPS | Command acknowledgements |

The demo publishes with **QoS 1**. The Mosquitto ACL limits the Pi identity to its own event, telemetry, status, and acknowledgement topics, and permits it to subscribe only to its command topic. The Django identity has the complementary permissions.

## Requirements

### VPS

- Linux server with Docker Engine and the Docker Compose plugin.
- A DNS `A` record for `server.echonode.website` pointing to the VPS.
- One of the supported reverse-proxy modes:
  - `traefik-swarm`: an existing Traefik deployment using a Docker Swarm overlay network (default).
  - `traefik-compose`: an existing Traefik Docker provider and shared Docker network.
  - `standalone`: the included Caddy service; ports 80 and 443 must be available.

### Raspberry Pi

- Raspberry Pi 4 running Raspberry Pi OS / Debian-based Linux.
- Python 3 and systemd (the installation script installs the required Python packages and service).
- Network access to `server.echonode.website` on TCP port 443.

## Configure and deploy the VPS

1. Clone this repository and enter its directory.
2. Create the environment file:

   ```bash
   cp .env.example .env
   ```

3. Edit `.env`. Replace every `REPLACE_...` value with a unique strong secret. Set the proxy mode and make sure the Traefik network, entrypoint, and certificate resolver match the existing VPS configuration.
4. Keep the MQTT usernames consistent with the usernames in `mosquitto/aclfile` (`raspberry-pi-01` and `django-server`) unless you also update the ACL file.
5. Deploy:

   ```bash
   chmod +x scripts/*.sh
   ./scripts/deploy_vps.sh
   ```

6. Create a Django administrator account:

   **Docker Compose modes:**

   ```bash
   docker compose exec web python manage.py createsuperuser
   ```

   **Docker Swarm mode:** find a running web task and execute the command inside its container:

   ```bash
   docker ps --filter label=com.docker.swarm.service.name=mqtt-iot_web
   docker exec -it <WEB_CONTAINER_ID> python manage.py createsuperuser
   ```

7. Open `https://server.echonode.website/` and sign in with the administrator account.

The deployment script creates the Mosquitto password database in its persistent volume. Do not publish raw MQTT ports `1883` or `9001` to the Internet; the public client connection is WSS through HTTPS on port 443 at `/mqtt`.

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

4. Set `MQTT_PASSWORD` to the same device password configured in the VPS `.env`. Set a separate strong `LOCAL_WEB_PASSWORD` and a random `FLASK_SECRET_KEY`.
5. Start the service:

   ```bash
   sudo systemctl start mqtt-iot-pi
   sudo systemctl status mqtt-iot-pi
   ```

6. Find the Pi's LAN address and open the local page from a device on the same network:

   ```bash
   hostname -I
   ```

   Browse to `http://<RASPBERRY_PI_IP>:8080/` and sign in with `LOCAL_WEB_PASSWORD`.

The local Flask interface is intended for a trusted LAN. Do not expose port 8080 directly to the public Internet.

## Try the demo

1. Open the Raspberry Pi local page and the VPS dashboard in separate browser tabs.
2. Send a text message from the Pi page. It should appear in the cloud dashboard's recent message list.
3. Publish the sample telemetry and verify that temperature/humidity values are recorded.
4. From the cloud dashboard, send an `on` or `off` output command.
5. The Pi receives the command and publishes an acknowledgement. The local page and cloud dashboard show the updated state.

For optional physical output, connect a low-voltage LED and a suitable current-limiting resistor to BCM GPIO17 and GND, then set `ENABLE_GPIO=1` in `/etc/mqtt-iot-pi.env`. Do not connect mains voltage or other high-power loads directly to a GPIO pin.

## Useful operations

```bash
# VPS logs (Docker Compose)
docker compose logs -f web listener broker

# VPS logs (Docker Swarm)
docker service logs -f mqtt-iot_web
docker service logs -f mqtt-iot_listener
docker service logs -f mqtt-iot_broker

# Raspberry Pi service logs
sudo journalctl -u mqtt-iot-pi -f

# Restart the Raspberry Pi service
sudo systemctl restart mqtt-iot-pi
```

## Repository layout

```text
.
├── dashboard/                 # Django app, MQTT listener, models, tests
├── deploy/                    # Raspberry Pi systemd unit
├── iotlab/                    # Django project configuration
├── mosquitto/                 # Broker configuration and topic ACL
├── pi_device/                 # Raspberry Pi Flask UI and MQTT client
├── scripts/                   # VPS deployment and Pi installation scripts
├── static/dashboard/          # Cloud dashboard CSS and JavaScript
├── templates/                 # Django templates
├── Dockerfile
├── docker-compose.yml
├── docker-compose.traefik.yml
├── docker-stack.traefik-swarm.yml
└── README.md
```

## Security notes

- Never commit `.env`, `/etc/mqtt-iot-pi.env`, generated Mosquitto password files, database files, or private keys.
- Use different strong passwords for the Raspberry Pi MQTT account, Django MQTT account, local Pi web login, and Django administrator.
- Keep the broker's raw MQTT and WebSocket listener ports private behind the reverse proxy.
- Restrict the Raspberry Pi local web page to a trusted network or place it behind an additional trusted access layer.
- Change the sample device identity and its ACL entries before adding multiple real devices.

## Tests and validation

Run the Django tests inside an environment with the project dependencies installed:

```bash
python manage.py test
```

The deployment also requires a live network test between the Raspberry Pi and the VPS. A successful static check does not replace deployment validation on the actual hardware and server.

## License

No license has been specified yet. Add a `LICENSE` file before granting others permission to reuse or redistribute this project.
