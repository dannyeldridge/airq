# AirQ Server Setup Guide

## 1. Initial Server Setup

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install required packages
sudo apt install python3 python3-pip python3-venv nginx git -y

# Create application directory
sudo mkdir -p /opt/airq
sudo chown $USER:$USER /opt/airq
```

## 2. Deploy Application

```bash
# Clone or deploy your code to /opt/airq
# Then set up Python environment
cd /opt/airq
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Configure Systemd Service

```bash
# Copy service file
sudo cp airq.service /etc/systemd/system/

# Edit the service file to customize:
sudo nano /etc/systemd/system/airq.service
# - Change SECRET_KEY to a random secure key
# - Adjust paths if needed

# Enable and start the service
sudo systemctl daemon-reload
sudo systemctl enable airq
sudo systemctl start airq

# Check status
sudo systemctl status airq
```

## 4. Configure Nginx

```bash
# Copy nginx config
sudo cp nginx-airq /etc/nginx/sites-available/airq

# Edit the config:
sudo nano /etc/nginx/sites-available/airq
# - Change server_name to your domain
# - Configure SSL if needed

# Enable the site
sudo ln -s /etc/nginx/sites-available/airq /etc/nginx/sites-enabled/
sudo nginx -t  # Test configuration
sudo systemctl restart nginx
```

## 5. Initialize Database

```bash
# Create database directory with proper permissions
sudo mkdir -p /var/db
sudo chown www-data:www-data /var/db
sudo chmod 775 /var/db

# Initialize database
cd /opt/airq
sudo -u www-data venv/bin/flask init-db
```

## 6. Configure Devices

```bash
# Add your air quality devices
sudo -u www-data venv/bin/flask device add airgradient "Living Room" \
  --token YOUR_API_TOKEN \
  --location YOUR_LOCATION_ID \
  --validate

# List devices to verify
sudo -u www-data venv/bin/flask device list
```

## 7. Verify Everything Works

```bash
# Check service status
sudo systemctl status airq

# Check nginx status  
sudo systemctl status nginx

# View logs if needed
sudo journalctl -u airq -f
sudo tail -f /var/log/nginx/access.log
```

## Environment Variables

The service uses these environment variables (configured in `.env` file):

- `DATABASE_PATH`: Where to store the database (example: `/var/db/airq.db`)
- `SECRET_KEY`: Flask secret key for sessions (CHANGE THIS!)

## File Permissions

- Application files: `www-data:www-data`
- Database directory: `www-data:www-data` with write access
- Service runs as: `www-data` user

## SSL/HTTPS Setup (Optional)

For production, set up SSL with Let's Encrypt:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

Then uncomment the HTTPS server block in the nginx config.