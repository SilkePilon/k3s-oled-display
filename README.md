# k3s Kubernetes OLED Display

128×64 SSD1306 OLED stats display for a Raspberry Pi connected to a k3s cluster.  
Cycles through **5 screens** every 5 seconds:

| #   | Screen          | Shows                                                            |
| --- | --------------- | ---------------------------------------------------------------- |
| 1   | **Overview**    | Nodes ready, pods running/total, namespaces, services, timestamp |
| 2   | **Pod Status**  | Running / Pending / Failed counts with progress bars             |
| 3   | **Node Detail** | Per-node name, CPU cores, RAM — cycles through all nodes         |
| 4   | **Deployments** | Ready/total deployments, health %, services, namespaces          |
| 5   | **Health**      | Pass/fail checklist for nodes, pods, and deployments             |

---

## Hardware

- Raspberry Pi (any model with I²C)
- SSD1306 128×64 OLED on I²C address `0x3C`
- Reset wire on **GPIO 4**

---

## Setup

### 1. Enable I²C

```bash
sudo raspi-config   # Interface Options → I2C → Enable
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Copy and fill in your kubeconfig

```bash
cp k3s-config.example.yaml k3s-config.yaml
```

Edit `k3s-config.yaml` and replace the placeholder values with your actual k3s cluster credentials:

- `<YOUR_K3S_SERVER_IP>` — your cluster's IP address
- `<BASE64_ENCODED_CA_CERT>` — base64-encoded CA certificate (from your k3s server)
- `<BASE64_ENCODED_CLIENT_CERT>` — base64-encoded client certificate
- `<BASE64_ENCODED_CLIENT_KEY>` — base64-encoded client private key

Then restrict permissions:

```bash
chmod 600 k3s-config.yaml
```

> **Tip:** On your k3s server the ready-to-use kubeconfig is at `/etc/rancher/k3s/k3s.yaml`.  
> Copy it to `k3s-config.yaml` on the Pi and update the `server:` address.

### 4. Run

```bash
python stats.py
```

To rotate the display 180°:

```bash
OLED_ROTATION=2 python stats.py
```

To use a different kubeconfig path:

```bash
KUBECONFIG=/path/to/your/config python stats.py
```

---

## Run as a systemd service

Create `/etc/systemd/system/k3s-oled.service`:

```ini
[Unit]
Description=k3s OLED Stats Display
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/k3s-oled-display/stats.py
WorkingDirectory=/home/pi/k3s-oled-display
Restart=on-failure
RestartSec=5
Environment=KUBECONFIG=/home/pi/k3s-oled-display/k3s-config.yaml

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now k3s-oled
```

---

## Tuning

| Variable          | Default  | Description                         |
| ----------------- | -------- | ----------------------------------- |
| `SCREEN_DURATION` | `5.0` s  | Time each screen is shown           |
| `FETCH_INTERVAL`  | `30.0` s | How often cluster data is refreshed |
| `OLED_ADDR`       | `0x3C`   | I²C address of the display          |
| `OLED_ROTATION`   | `1`      | Set `2` for 180° rotation           |
