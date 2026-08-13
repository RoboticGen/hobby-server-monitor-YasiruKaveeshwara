# Deployment Notes (Systemd Setup)

Follow these exact commands to install and start the systemd services on the host machine.

### 1. Create the dedicated service account

As per decision 7.4, the backend and collector run as a dedicated low-privilege OS user that belongs to the `lxd` group and nothing else.

```bash
sudo useradd --system --no-create-home hsm-runner
sudo usermod -aG lxd hsm-runner
```

### 2. Configure file permissions

Because the services run as the dedicated `hsm-runner` user, it needs permission to traverse into your home directory, and it must own the `data/` directory to write the database and metrics files.

```bash
# Allow traversal into the home directory
chmod o+x /home/yasiru

# Transfer ownership of the data directory so the API and Collector can write
sudo chown -R hsm-runner:hsm-runner /home/yasiru/projects/hobby-server-monitor-YasiruKaveeshwara/data/
```

### 3. Install the unit files

Link or copy the unit files into the systemd directory:

```bash
sudo ln -s /home/yasiru/projects/hobby-server-monitor-YasiruKaveeshwara/deploy/hsm-api.service /etc/systemd/system/hsm-api.service
sudo ln -s /home/yasiru/projects/hobby-server-monitor-YasiruKaveeshwara/deploy/hsm-collector.service /etc/systemd/system/hsm-collector.service
sudo systemctl daemon-reload
```

### 4. Enable and start the services

```bash
sudo systemctl enable hsm-api.service
sudo systemctl start hsm-api.service

sudo systemctl enable hsm-collector.service
sudo systemctl start hsm-collector.service
```

### 5. Verify

```bash
sudo systemctl status hsm-api.service
sudo systemctl status hsm-collector.service
```
