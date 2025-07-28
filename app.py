import os
import sqlite3
import requests
import time
import json
import click
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify
import threading
import logging
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
app.config["DATABASE"] = os.environ.get("DATABASE_PATH", "./airq.db")

# Fail hard if SECRET_KEY is missing in production
if not app.config["SECRET_KEY"]:
    raise ValueError("SECRET_KEY must be set in environment variables")

# Global variable to track if background thread is started
_background_thread_started = False

# Configuration
FETCH_INTERVAL = 60


def get_configured_devices():
    """Load devices from database."""
    conn = sqlite3.connect(app.config["DATABASE"], check_same_thread=False)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, name, provider, config 
        FROM devices 
        WHERE active = 1
        ORDER BY id
        """
    )

    devices = []
    for row in cursor.fetchall():
        device_id, name, provider, config_json = row
        config = json.loads(config_json)
        devices.append(
            {"id": device_id, "name": name, "provider": provider, "config": config}
        )

    conn.close()
    return devices


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class DeviceAdapter(ABC):
    """Abstract base class for device adapters."""

    def __init__(self, device_config):
        self.device_config = device_config
        self.name = device_config["name"]
        self.device_id = device_config["id"]

    @abstractmethod
    def fetch_data(self):
        """Fetch data from device and return standardized measurement dict."""
        pass

    def get_device_info(self):
        """Return device metadata."""
        return {
            "id": self.device_id,
            "name": self.name,
            "provider": self.device_config["provider"],
        }


class AirGradientAdapter(DeviceAdapter):
    """Adapter for AirGradient devices."""

    def __init__(self, device_config):
        super().__init__(device_config)
        self.api_token = device_config["config"]["api_token"]
        self.location_id = device_config["config"]["location_id"]
        self.api_base_url = "https://api.airgradient.com/public/api/v1"

    def fetch_data(self):
        """Fetch data from AirGradient API."""
        try:
            url = f"{self.api_base_url}/locations/{self.location_id}/measures/current?token={self.api_token}"
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            data = response.json()
            logger.info(f"Fetched data from {self.name}: {data}")

            # Convert AirGradient format to standardized format
            return {
                "device_id": self.device_id,
                "timestamp": data.get("timestamp"),
                "pm1": data.get("pm01"),
                "pm2": data.get("pm02"),
                "pm10": data.get("pm10"),
                "co2": data.get("rco2"),
                "temperature": data.get("atmp"),
                "humidity": data.get("rhum"),
                "nox": data.get("noxIndex"),
                "tvoc": data.get("tvocIndex"),
            }

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching data from {self.name}: {e}")
            return None


def create_device_adapter(device_config):
    """Factory function to create appropriate device adapter."""
    device_provider = device_config["provider"]

    if device_provider == "airgradient":
        return AirGradientAdapter(device_config)
    else:
        raise ValueError(f"Unknown device provider: {device_provider}")


def init_database():
    """Initialize the SQLite database with required tables."""
    conn = sqlite3.connect(app.config["DATABASE"], check_same_thread=False)
    cursor = conn.cursor()
    
    # Enable WAL mode for better concurrent access
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")

    # Create devices table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            provider TEXT NOT NULL,
            config JSON,
            active BOOLEAN DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """
    )

    # Create measurements table with device_id
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            pm1 REAL,
            pm2 REAL,
            pm10 REAL,
            co2 INTEGER,
            temperature REAL,
            humidity REAL,
            nox INTEGER,
            tvoc INTEGER,
            FOREIGN KEY (device_id) REFERENCES devices (id)
        )
    """
    )

    # Create indexes for faster queries
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_timestamp ON measurements(timestamp)
    """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_device_timestamp ON measurements(device_id, timestamp)
    """
    )

    # Database initialization complete - devices will be added via CLI

    conn.commit()
    conn.close()
    logger.info("Database initialized")


def store_measurement(data):
    """Store measurement data in SQLite database."""
    if not data:
        return

    conn = sqlite3.connect(app.config["DATABASE"], check_same_thread=False)
    cursor = conn.cursor()

    try:
        # Store UTC timestamps
        api_timestamp = data.get("timestamp")
        if api_timestamp:
            # Parse UTC timestamp from API and store as UTC
            utc_dt = datetime.fromisoformat(api_timestamp.replace("Z", "+00:00"))
            timestamp_str = utc_dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        else:
            # Fallback to current UTC time
            timestamp_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute(
            """
            INSERT INTO measurements 
            (device_id, timestamp, pm1, pm2, pm10, co2, temperature, humidity, nox, tvoc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                data.get("device_id"),
                timestamp_str,
                data.get("pm1"),
                data.get("pm2"),
                data.get("pm10"),
                data.get("co2"),
                data.get("temperature"),
                data.get("humidity"),
                data.get("nox"),
                data.get("tvoc"),
            ),
        )

        conn.commit()
        logger.info(
            f"Measurement stored successfully for device {data.get('device_id')} at {timestamp_str} UTC"
        )

    except sqlite3.Error as e:
        logger.error(f"Database error: {e}")
    finally:
        conn.close()


def data_fetcher():
    """Background thread function to fetch data periodically from all devices."""
    while True:
        try:
            devices = get_configured_devices()
            if not devices:
                logger.warning(
                    "No devices configured. Use 'flask device add' to add devices."
                )
            else:
                for device_config in devices:
                    try:
                        adapter = create_device_adapter(device_config)
                        data = adapter.fetch_data()
                        if data:
                            store_measurement(data)
                        else:
                            logger.warning(
                                f"No data received from {device_config['name']}"
                            )
                    except Exception as e:
                        logger.error(
                            f"Error fetching data from {device_config['name']}: {e}"
                        )
        except Exception as e:
            logger.error(f"Error in data fetcher: {e}")

        time.sleep(FETCH_INTERVAL)


def start_background_thread():
    """Start the background data fetcher thread if not already started."""
    global _background_thread_started
    if not _background_thread_started:
        # Initialize database
        init_database()

        # Start background data fetcher
        fetcher_thread = threading.Thread(target=data_fetcher, daemon=True)
        fetcher_thread.start()
        logger.info("Background data fetcher started")

        _background_thread_started = True


def get_devices():
    """Get all active devices."""
    rows = execute_db_query(
        """
        SELECT id, name, provider FROM devices 
        WHERE active = 1
        ORDER BY name
        """,
        fetch_all=True,
    )

    return [{"id": row[0], "name": row[1], "provider": row[2]} for row in rows]


def get_latest_measurement(device_id=None):
    """Get the most recent measurement from database."""
    conn = sqlite3.connect(app.config["DATABASE"], check_same_thread=False)
    cursor = conn.cursor()

    if device_id:
        cursor.execute(
            """
            SELECT m.*, d.name as device_name FROM measurements m
            JOIN devices d ON m.device_id = d.id 
            WHERE m.device_id = ?
            ORDER BY m.timestamp DESC 
            LIMIT 1
        """,
            (device_id,),
        )
    else:
        # Get latest from any device
        cursor.execute(
            """
            SELECT m.*, d.name as device_name FROM measurements m
            JOIN devices d ON m.device_id = d.id
            ORDER BY m.timestamp DESC 
            LIMIT 1
        """
        )

    row = cursor.fetchone()
    conn.close()

    if row:
        return {
            "id": row[0],
            "device_id": row[1],
            "timestamp": row[2],  # UTC timestamp from storage
            "pm1": row[3],
            "pm2": row[4],
            "pm10": row[5],
            "co2": row[6],
            "temperature": row[7],
            "humidity": row[8],
            "nox": row[9],
            "tvoc": row[10],
            "device_name": row[11],
        }
    return None


def get_historical_data(hours=24, device_id=None):
    """Get historical data for the specified number of hours."""
    conn = sqlite3.connect(app.config["DATABASE"], check_same_thread=False)
    cursor = conn.cursor()

    since = datetime.utcnow() - timedelta(hours=hours)

    if device_id:
        cursor.execute(
            """
            SELECT m.timestamp, m.pm2, m.co2, m.temperature, m.humidity, m.tvoc, m.nox, d.name as device_name
            FROM measurements m
            JOIN devices d ON m.device_id = d.id
            WHERE m.timestamp > ? AND m.device_id = ?
            ORDER BY m.timestamp ASC
        """,
            (since, device_id),
        )
    else:
        cursor.execute(
            """
            SELECT m.timestamp, m.pm2, m.co2, m.temperature, m.humidity, m.tvoc, m.nox, d.name as device_name
            FROM measurements m
            JOIN devices d ON m.device_id = d.id
            WHERE m.timestamp > ? 
            ORDER BY m.timestamp ASC
        """,
            (since,),
        )

    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "timestamp": row[0],  # UTC timestamp from storage
            "pm2": row[1],
            "co2": row[2],
            "temperature": row[3],
            "humidity": row[4],
            "tvoc": row[5],
            "nox": row[6],
            "device_name": row[7],
        }
        for row in rows
    ]


@app.route("/")
def dashboard():
    """Main dashboard page."""
    # Start background thread on first request
    start_background_thread()
    return render_template("dashboard.html")


@app.route("/api/devices")
def api_devices():
    """API endpoint for device list."""
    devices = get_devices()
    return jsonify(devices)


@app.route("/api/current")
@app.route("/api/current/<int:device_id>")
def api_current(device_id=None):
    """API endpoint for current measurements."""
    # Validate device_id if provided
    if device_id is not None:
        if device_id < 1:
            return jsonify({"error": "Device ID must be a positive integer"}), 400
        
        # Check if device exists and is active
        device_exists = execute_db_query(
            "SELECT 1 FROM devices WHERE id = ? AND active = 1",
            (device_id,),
            fetch_one=True
        )
        if not device_exists:
            return jsonify({"error": f"Active device with ID {device_id} not found"}), 404
    
    data = get_latest_measurement(device_id)
    if data:
        return jsonify(data)
    return jsonify({"error": "No data available"}), 404


@app.route("/api/history/<int:hours>")
@app.route("/api/history/<int:hours>/<int:device_id>")
def api_history(hours, device_id=None):
    """API endpoint for historical data."""
    # Validate hours parameter
    if hours < 1 or hours > 168:
        return jsonify({"error": "Hours must be between 1 and 168"}), 400
    
    # Validate device_id if provided
    if device_id is not None:
        if device_id < 1:
            return jsonify({"error": "Device ID must be a positive integer"}), 400
        
        # Check if device exists and is active
        device_exists = execute_db_query(
            "SELECT 1 FROM devices WHERE id = ? AND active = 1",
            (device_id,),
            fetch_one=True
        )
        if not device_exists:
            return jsonify({"error": f"Active device with ID {device_id} not found"}), 404

    data = get_historical_data(hours, device_id)
    return jsonify(data)


@app.route("/health")
def health_check():
    """Health check endpoint."""
    # Ensure background thread is running
    start_background_thread()
    return jsonify({"status": "healthy", "timestamp": datetime.utcnow().isoformat()})


@app.route("/debug")
def debug_timestamps():
    """Debug endpoint to see raw database timestamps."""
    conn = sqlite3.connect(app.config["DATABASE"], check_same_thread=False)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT m.timestamp, m.pm2, m.co2, d.name as device_name
        FROM measurements m
        JOIN devices d ON m.device_id = d.id
        ORDER BY m.timestamp DESC 
        LIMIT 5
    """
    )

    rows = cursor.fetchall()
    conn.close()

    current_utc_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    return jsonify(
        {
            "current_utc_time": current_utc_time,
            "recent_records": [
                {
                    "timestamp": row[0],
                    "pm2": row[1],
                    "co2": row[2],
                    "device_name": row[3],
                }
                for row in rows
            ],
        }
    )


# Flask CLI Commands
@app.cli.group()
def device():
    """Device management commands."""
    pass


@device.command("add")
@click.argument("provider")
@click.argument("name")
@click.option("--token", help="API token (required for airgradient)")
@click.option("--location", help="Location ID (required for airgradient)")
@click.option("--validate", is_flag=True, help="Validate connection before adding")
@click.option("--force", is_flag=True, help="Add device even if validation fails")
def add_device(provider, name, token, location, validate, force):
    """Add a new device."""
    # Validate provider
    if provider not in ["airgradient"]:
        app.logger.error(f"Unknown provider '{provider}'. Supported: airgradient")
        return

    # Build config based on provider
    config = {}
    if provider == "airgradient":
        if not token or not location:
            app.logger.error("AirGradient devices require --token and --location")
            return
        config = {"api_token": token, "location_id": location}

    # Get next available ID
    max_id = execute_db_query("SELECT MAX(id) FROM devices", fetch_one=True)[0]
    device_id = (max_id or 0) + 1

    # Validate connection if requested
    if validate:
        print("Validating device connection...")
        test_config = {
            "id": device_id,
            "name": name,
            "provider": provider,
            "config": config,
        }
        try:
            adapter = create_device_adapter(test_config)
            data = adapter.fetch_data()
            if data:
                print("✓ Connection validation successful!")
            else:
                print("✗ Connection validation failed - no data received")
                if not force:
                    print("Use --force to add device anyway")
                    return
        except Exception as e:
            print(f"✗ Connection validation failed: {e}")
            if not force:
                print("Use --force to add device anyway")
                return

    # Insert device
    try:
        execute_db_query(
            """
            INSERT INTO devices (id, name, provider, config, active)
            VALUES (?, ?, ?, ?, ?)
            """,
            (device_id, name, provider, json.dumps(config), True),
        )
        print(f"✓ Device '{name}' added successfully (ID: {device_id})")
    except sqlite3.Error as e:
        print(f"Error adding device: {e}")


@device.command("list")
@click.option("--all", is_flag=True, help="Show inactive devices as well")
def list_devices(all):
    """List devices."""
    if all:
        devices = execute_db_query(
            """
            SELECT id, name, provider, active, created_at 
            FROM devices 
            ORDER BY active DESC, id
            """,
            fetch_all=True,
        )
    else:
        devices = execute_db_query(
            """
            SELECT id, name, provider, active, created_at 
            FROM devices 
            WHERE active = 1
            ORDER BY id
            """,
            fetch_all=True,
        )

    if not devices:
        status_text = "No devices configured." if all else "No active devices. Use --all to see inactive devices."
        print(status_text)
        return

    print(f"{'ID':<4} {'Name':<20} {'Provider':<12} {'Status':<8} {'Created'}")
    print("-" * 60)

    for device in devices:
        device_id, name, provider, active, created_at = device
        status = "Active" if active else "Inactive"
        created = (
            datetime.fromisoformat(created_at).strftime("%Y-%m-%d")
            if created_at
            else "Unknown"
        )
        print(f"{device_id:<4} {name:<20} {provider:<12} {status:<8} {created}")


@device.command("remove")
@click.argument("device_id", type=int)
@click.option("--force", is_flag=True, help="Skip confirmation")
def remove_device(device_id, force):
    """Remove a device."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Check if device exists
    cursor.execute("SELECT name FROM devices WHERE id = ?", (device_id,))
    device = cursor.fetchone()

    if not device:
        print(f"Error: Device with ID {device_id} not found")
        conn.close()
        return

    device_name = device[0]

    # Confirm deletion unless --force
    if not force:
        response = input(f"Remove device '{device_name}' (ID: {device_id})? [y/N]: ")
        if response.lower() != "y":
            print("Cancelled.")
            conn.close()
            return

    # Remove device
    try:
        cursor.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        conn.commit()
        print(f"✓ Device '{device_name}' removed successfully")
    except sqlite3.Error as e:
        print(f"Error removing device: {e}")
    finally:
        conn.close()


@device.command("test")
@click.argument("device_id", type=int)
def test_device(device_id):
    """Test device connection."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT name, provider, config FROM devices WHERE id = ? AND active = 1",
        (device_id,),
    )

    device = cursor.fetchone()
    conn.close()

    if not device:
        print(f"Error: Active device with ID {device_id} not found")
        return

    name, provider, config_json = device
    config = json.loads(config_json)

    print(f"Testing device '{name}' (Provider: {provider})...")

    test_config = {
        "id": device_id,
        "name": name,
        "provider": provider,
        "config": config,
    }

    try:
        adapter = create_device_adapter(test_config)
        data = adapter.fetch_data()

        if data:
            print("✓ Connection successful!")
            print(
                f"Sample data: PM2.5={data.get('pm2')}, CO2={data.get('co2')}, Temp={data.get('temperature')}"
            )
        else:
            print("✗ Connection failed - no data received")
    except Exception as e:
        print(f"✗ Connection failed: {e}")


@device.command("deactivate")
@click.argument("device_id", type=int)
@click.option("--force", is_flag=True, help="Skip confirmation")
def deactivate_device(device_id, force):
    """Deactivate a device (stops data collection but keeps history)."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Check if device exists
    cursor.execute("SELECT name, active FROM devices WHERE id = ?", (device_id,))
    device = cursor.fetchone()

    if not device:
        print(f"Error: Device with ID {device_id} not found")
        conn.close()
        return

    device_name, is_active = device

    if not is_active:
        print(f"Device '{device_name}' (ID: {device_id}) is already inactive")
        conn.close()
        return

    # Confirm deactivation unless --force
    if not force:
        response = input(f"Deactivate device '{device_name}' (ID: {device_id})? Data collection will stop but history will be preserved. [y/N]: ")
        if response.lower() != "y":
            print("Cancelled.")
            conn.close()
            return

    # Deactivate device
    try:
        cursor.execute("UPDATE devices SET active = 0 WHERE id = ?", (device_id,))
        conn.commit()
        print(f"✓ Device '{device_name}' deactivated successfully")
        print("  Data collection stopped. Historical data preserved.")
    except sqlite3.Error as e:
        print(f"Error deactivating device: {e}")
    finally:
        conn.close()


@device.command("activate")
@click.argument("device_id", type=int)
def activate_device(device_id):
    """Activate a previously deactivated device."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Check if device exists
    cursor.execute("SELECT name, active FROM devices WHERE id = ?", (device_id,))
    device = cursor.fetchone()

    if not device:
        print(f"Error: Device with ID {device_id} not found")
        conn.close()
        return

    device_name, is_active = device

    if is_active:
        print(f"Device '{device_name}' (ID: {device_id}) is already active")
        conn.close()
        return

    # Activate device
    try:
        cursor.execute("UPDATE devices SET active = 1 WHERE id = ?", (device_id,))
        conn.commit()
        print(f"✓ Device '{device_name}' activated successfully")
        print("  Data collection will resume.")
    except sqlite3.Error as e:
        print(f"Error activating device: {e}")
    finally:
        conn.close()


@app.cli.command("init-db")
def init_db_command():
    """Initialize the database."""
    print("Initializing database...")
    try:
        init_database()
        print("✓ Database initialized successfully")
    except Exception as e:
        print(f"Error initializing database: {e}")


def get_db_connection():
    """Get database connection."""
    return sqlite3.connect(app.config["DATABASE"], check_same_thread=False)


def execute_db_query(query, params=None, fetch_one=False, fetch_all=False):
    """Execute database query with automatic connection handling."""
    with sqlite3.connect(app.config["DATABASE"], check_same_thread=False) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params or ())

        if fetch_one:
            return cursor.fetchone()
        elif fetch_all:
            return cursor.fetchall()

        conn.commit()
        return cursor


# For gunicorn compatibility - only start background thread in single-worker mode
# In production, use --preload and single worker to avoid multiple threads
import sys
if not any('flask' in arg for arg in sys.argv):
    # Only start if not running under gunicorn with multiple workers
    # Gunicorn sets GUNICORN_WORKER environment variable
    if os.environ.get('GUNICORN_WORKER') != 'true':
        with app.app_context():
            start_background_thread()

if __name__ == "__main__":
    # For development - already started above
    # Run Flask app
    app.run(host="0.0.0.0", port=5001, debug=True)
