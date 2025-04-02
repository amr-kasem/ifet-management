#!/usr/bin/env python3
import requests
import paho.mqtt.client as mqtt
import time
import json
import logging
import os
import sys
from typing import Dict, Any, Optional, List, Tuple
import threading
from pathlib import Path

# Try to load .env file if it exists
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent / '.env'
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
        print(f"Loaded environment from {env_path}")
except ImportError:
    print("python-dotenv not installed. Using environment variables directly.")
except Exception as e:
    print(f"Error loading .env file: {e}")

# Configure logging
log_level_str = os.getenv("LOG_LEVEL", "INFO")
log_level = getattr(logging, log_level_str.upper(), logging.INFO)
logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("sick_gateway")

# Constants for unit conversion
MM_TO_INCH = 0.0393701  # 1 mm = 0.0393701 inches

class SickGateway:
    def __init__(self):
        # Configuration
        self.sick_api_host = os.getenv("SICK_API_HOST", "sick_api_simulator")
        self.sick_api_port = int(os.getenv("SICK_API_PORT", "3197"))
        self.mqtt_broker = os.getenv("MQTT_BROKER", "mosquitto")
        self.mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        self.poll_interval = int(os.getenv("POLL_INTERVAL", "1"))  # seconds
        self.config_file = os.getenv("CONFIG_FILE", "config.json")
        
        # Unit of measurement setting
        self.unit = os.getenv("UNIT_OF_MEASUREMENT", "mm").lower()
        if self.unit not in ["mm", "inch"]:
            logger.warning(f"Invalid unit of measurement: {self.unit}. Using 'mm' as default.")
            self.unit = "mm"
        logger.info(f"Unit of measurement set to: {self.unit}")
        
        # Device information and state
        self.devices = {}  # Will store device_id -> device_info
        self.zero_offsets = {}  # Will store device_id -> offset_value
        self.device_list = []  # Will store (device_id, master_id, port_id) tuples
        
        # Assignment tracking
        self.assignments = {}  # Will store device_id -> testing_system_id
        self.max_values = {}   # Will store device_id -> max_value since assignment
        self.latest_values = {}  # Will store device_id -> latest_value since assignment
        
        # Load device configuration
        self.load_device_configuration()
        
        # Connect to MQTT broker
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_message = self.on_message
        
        self.running = False

    def convert_units(self, value: float, to_unit: str = None) -> float:
        """Convert value between mm and inches based on the current unit setting"""
        if to_unit is None:
            to_unit = self.unit
            
        # If the target unit is mm, no conversion needed (sensor readings are in mm)
        if to_unit == "mm":
            return value
        
        # If the target unit is inch, convert from mm to inch
        elif to_unit == "inch":
            return round(value * MM_TO_INCH, 4)  # Round to 4 decimal places
        
        # If invalid unit, return the original value
        else:
            logger.warning(f"Invalid unit for conversion: {to_unit}")
            return value

    def load_device_configuration(self):
        """Load device configuration from file or environment variables"""
        # Try to load from config file first
        config_loaded = False
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                
                if 'devices' in config and isinstance(config['devices'], list):
                    for device in config['devices']:
                        if all(k in device for k in ['id', 'master_id', 'port_id']):
                            self.device_list.append((
                                device['id'],
                                device['master_id'],
                                device['port_id']
                            ))
                    
                    if self.device_list:
                        logger.info(f"Loaded {len(self.device_list)} devices from config file")
                        config_loaded = True
                    else:
                        logger.warning("No valid devices found in config file")
                else:
                    logger.warning("Config file missing 'devices' section or not in correct format")
            
            except json.JSONDecodeError:
                logger.error(f"Error parsing config file {self.config_file}")
            except Exception as e:
                logger.error(f"Error loading config file: {str(e)}")
        
        # If config file didn't work, try environment variables
        if not config_loaded:
            devices_env = os.getenv("SICK_DEVICES", "")
            if devices_env:
                try:
                    # Format: "device1:1:1,device2:1:2,device3:2:1"
                    # device_id:master_id:port_id
                    devices = devices_env.split(',')
                    for device_str in devices:
                        parts = device_str.strip().split(':')
                        if len(parts) == 3:
                            self.device_list.append((parts[0], parts[1], parts[2]))
                    
                    if self.device_list:
                        logger.info(f"Loaded {len(self.device_list)} devices from environment variable")
                        config_loaded = True
                    else:
                        logger.warning("No valid devices found in environment variable")
                
                except Exception as e:
                    logger.error(f"Error parsing devices from environment variable: {str(e)}")
        
        # If no configuration was loaded, use default devices
        if not config_loaded:
            logger.warning("No device configuration found, using defaults")
            self.device_list = [
                ("device1", "1", "1"),
                ("device2", "1", "2"),
            ]
        
        # Log the configured devices
        logger.info("Configured devices:")
        for device_id, master_id, port_id in self.device_list:
            logger.info(f"  Device: {device_id}, Master ID: {master_id}, Port ID: {port_id}")
            # Initialize assignment and tracking values for each device
            self.assignments[device_id] = None
            self.max_values[device_id] = None
            self.latest_values[device_id] = None

    def on_connect(self, client, userdata, flags, rc):
        logger.info(f"Connected to MQTT broker with result code {rc}")
        # Subscribe to commands topic for all devices
        client.subscribe("sick/commands/#")
        # Subscribe to assignment topics
        client.subscribe("sick/assign/#")
        client.subscribe("sick/release/#")

    def on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages"""
        topic = msg.topic
        payload = msg.payload.decode('utf-8')
        
        logger.info(f"Received message on topic {topic}: {payload}")
        
        # Parse command topics
        if topic.startswith("sick/commands/"):
            try:
                # Extract device ID from topic
                parts = topic.split("/")
                if len(parts) < 3:
                    logger.warning(f"Invalid command topic format: {topic}")
                    return
                
                device_id = parts[2]
                
                # Parse command payload
                command_data = json.loads(payload)
                command = command_data.get("command")
                
                # Zero commands are no longer supported independently
                if command == "zero" or command == "reset_zero":
                    logger.warning(f"Independent zero commands are deprecated. Zero function is now handled automatically during assignment.")
                    
                    # Publish response indicating command is deprecated
                    self.mqtt_client.publish(
                        f"sick/sensors/{device_id}/zero_status",
                        json.dumps({
                            "status": "error", 
                            "message": "Zero commands are deprecated. Zeroing happens automatically when a sensor is assigned."
                        })
                    )
                else:
                    logger.warning(f"Unknown command: {command}")
            
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON payload: {payload}")
            except Exception as e:
                logger.error(f"Error processing command: {str(e)}")
                
        # Handle assignment commands
        elif topic.startswith("sick/assign/"):
            try:
                # Extract device ID from topic
                parts = topic.split("/")
                if len(parts) < 3:
                    logger.warning(f"Invalid assign topic format: {topic}")
                    return
                
                device_id = parts[2]
                
                # Parse assignment payload
                assign_data = json.loads(payload)
                testing_system_id = assign_data.get("testing_system_id")
                
                if testing_system_id:
                    self.handle_assign_command(device_id, testing_system_id)
                else:
                    logger.warning(f"Missing testing_system_id in assign payload")
            
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON payload: {payload}")
            except Exception as e:
                logger.error(f"Error processing assign command: {str(e)}")
                
        # Handle release commands
        elif topic.startswith("sick/release/"):
            try:
                # Extract device ID from topic
                parts = topic.split("/")
                if len(parts) < 3:
                    logger.warning(f"Invalid release topic format: {topic}")
                    return
                
                device_id = parts[2]
                self.handle_release_command(device_id)
            
            except Exception as e:
                logger.error(f"Error processing release command: {str(e)}")

    def handle_assign_command(self, device_id: str, testing_system_id: str):
        """Assign a device to a testing system and zero its value"""
        if device_id not in self.devices:
            logger.warning(f"Unknown device ID for assign command: {device_id}")
            return
        
        # Store the assignment
        self.assignments[device_id] = testing_system_id
        self.max_values[device_id] = None  # Reset max value tracking
        self.latest_values[device_id] = None  # Reset latest value tracking
        
        # Zero the device at assignment (this functionality is preserved)
        # Get the current raw value to use as offset
        current_value = self.devices[device_id].get("raw_value", 0)
        self.zero_offsets[device_id] = current_value
        logger.info(f"Device {device_id} assigned to testing system {testing_system_id} and zeroed. Offset: {current_value}")
        
        # Publish assignment and zero status
        self.mqtt_client.publish(
            f"sick/sensors/{device_id}/assignment_status",
            json.dumps({
                "assigned_to": testing_system_id,
                "assigned_at": time.time(),
                "zeroed": True,
                "offset": current_value,
                "permanent_value": 0  # The permanent_value after zeroing is 0
            })
        )

    def handle_release_command(self, device_id: str):
        """Release a device from a testing system"""
        if device_id not in self.assignments:
            logger.warning(f"Unknown device ID for release command: {device_id}")
            return
        
        # Get the current max value before releasing
        max_value = self.max_values.get(device_id)
        if max_value is not None:
            max_value = self.convert_units(max_value)
            
        # Get the latest permanent value before releasing
        permanent_value = self.latest_values.get(device_id)
        if permanent_value is not None:
            permanent_value = self.convert_units(permanent_value)
            
        previous_assignment = self.assignments.get(device_id, "free")
        
        # Clear the assignment and tracking values
        self.assignments[device_id] = None
        # Don't clear the max_values and latest_values to maintain history
        # self.max_values[device_id] = None
        # self.latest_values[device_id] = None
        
        # Also clear the zero offset when releasing
        if device_id in self.zero_offsets:
            del self.zero_offsets[device_id]
            logger.info(f"Zero offset reset for device {device_id} upon release")
        
        logger.info(f"Device {device_id} released from testing system. Max value was: {max_value}, Latest value was: {permanent_value}")
        
        # Publish release status with max and latest values
        self.mqtt_client.publish(
            f"sick/sensors/{device_id}/assignment_status",
            json.dumps({
                "assigned_to": "free",
                "released_at": time.time(),
                "previous_assignment": previous_assignment,
                "max_value": max_value,
                "permanent_value": permanent_value,
                "zeroed": False
            })
        )

    # Note: The handle_zero_command and handle_reset_zero_command methods are kept 
    # for internal use only. They're called by handle_assign_command and handle_release_command
    # but are not directly exposed via MQTT commands anymore.
    
    def handle_zero_command(self, device_id: str):
        """Set the current value as zero offset for a device (internal use only)"""
        if device_id not in self.devices:
            logger.warning(f"Unknown device ID for zero command: {device_id}")
            return
        
        # Get the current raw value to use as offset
        current_value = self.devices[device_id].get("raw_value", 0)
        self.zero_offsets[device_id] = current_value
        logger.info(f"Zero function applied to device {device_id}. Offset: {current_value}")
        
        # Publish confirmation is now handled by the calling method

    def handle_reset_zero_command(self, device_id: str):
        """Reset the zero offset for a device (internal use only)"""
        if device_id in self.zero_offsets:
            del self.zero_offsets[device_id]
            logger.info(f"Zero offset reset for device {device_id}")
            
            # Publish confirmation is now handled by the calling method
        else:
            logger.warning(f"No zero offset to reset for device {device_id}")

    def fetch_device_data(self, master_id: str, port_id: str) -> Optional[Dict[str, Any]]:
        """Fetch data from the SICK API simulator"""
        try:
            url = f"http://{self.sick_api_host}:{self.sick_api_port}/iolink/v1/devices/{master_id}{port_id}/processdata/getdata/value"
            response = requests.get(url, timeout=5)
            
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Error fetching device data: {response.status_code} - {response.text}")
                return None
        
        except requests.RequestException as e:
            logger.error(f"Request error while fetching device data: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error while fetching device data: {str(e)}")
            return None

    def process_device_data(self, device_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Process the raw device data and apply zero offset if needed"""
        if not data or "iolink" not in data or not data["iolink"].get("valid", False):
            return {"error": "Invalid data", "valid": False}
        
        raw_value = data["iolink"]["value"]
        
        # Extract the actual value from the array (assuming first two bytes represent the value)
        # This is a simplified example - adjust according to your specific data format
        if len(raw_value) >= 2:
            value = (raw_value[0] << 8) + raw_value[1]
        else:
            value = 0
        
        # Store the raw value (in mm)
        raw_value_numeric = value
        
        # Apply zero offset if exists
        offset = self.zero_offsets.get(device_id, 0)
        adjusted_value_mm = value - offset
        
        # Track max value if device is assigned to a testing system (track in mm)
        if self.assignments.get(device_id) is not None:
            current_max = self.max_values.get(device_id)
            if current_max is None or abs(adjusted_value_mm) > abs(current_max):
                self.max_values[device_id] = adjusted_value_mm
                logger.debug(f"New max value for device {device_id}: {adjusted_value_mm} mm")
        
        # Track latest value if device is assigned to a testing system (track in mm)
        if self.assignments.get(device_id) is not None:
            # Always update the latest value to the current value when assigned
            self.latest_values[device_id] = adjusted_value_mm
            logger.debug(f"Updated latest value for device {device_id}: {adjusted_value_mm} mm")
        
        # Convert values to the selected unit for output
        adjusted_value = self.convert_units(adjusted_value_mm)
        raw_value_converted = self.convert_units(raw_value_numeric)
        offset_converted = self.convert_units(offset)
        max_value = None if self.max_values.get(device_id) is None else self.convert_units(self.max_values.get(device_id))
        permanent_value = None if self.latest_values.get(device_id) is None else self.convert_units(self.latest_values.get(device_id))
        assigned_to = self.assignments.get(device_id, None)
        if assigned_to == None:
            assigned_to = "free"
        processed_data = {
            "valid": True,
            "raw_value": raw_value_converted,
            "value": adjusted_value,
            "permanent_value": permanent_value,  # Use the tracked permanent_value
            "offset": offset_converted,
            "zeroed": device_id in self.zero_offsets,
            "assigned_to": assigned_to,
            "max_value": max_value,
            "units": self.unit,
            "timestamp": time.time()
        }
        
        # Store the original values in mm for internal use (not the converted ones)
        internal_data = {
            "valid": True,
            "raw_value": raw_value_numeric,  # Original in mm
            "value": adjusted_value_mm,      # Original in mm
            "permanent_value": self.latest_values.get(device_id),  # Use the tracked permanent_value in mm
            "offset": offset,                # Original in mm
            "zeroed": device_id in self.zero_offsets,
            "assigned_to": assigned_to,
            "max_value": self.max_values.get(device_id),  # Original in mm
            "timestamp": time.time()
        }
        
        # Store the internal data for future reference
        self.devices[device_id] = internal_data
        
        return processed_data

    def poll_devices(self):
        """Poll devices and publish their data to MQTT"""
        while self.running:
            for device_id, master_id, port_id in self.device_list:
                # Fetch data from the SICK API
                data = self.fetch_device_data(master_id, port_id)
                
                if data:
                    # Process the data and apply zero offset if needed
                    processed_data = self.process_device_data(device_id, data)
                    
                    # Publish to MQTT
                    self.mqtt_client.publish(
                        f"sick/sensors/{device_id}",
                        json.dumps(processed_data)
                    )
                    
                    logger.debug(f"Published data for device {device_id}")
            
            # Wait for the next polling cycle
            time.sleep(self.poll_interval)

    def start(self):
        """Start the gateway"""
        try:
            logger.info("Starting SICK MQTT Gateway")
            
            # Connect to MQTT broker
            self.mqtt_client.connect(self.mqtt_broker, self.mqtt_port, 60)
            
            # Start MQTT loop in a background thread
            self.mqtt_client.loop_start()
            
            # Start polling thread
            self.running = True
            polling_thread = threading.Thread(target=self.poll_devices)
            polling_thread.daemon = True
            polling_thread.start()
            
            logger.info("Gateway started successfully")
            
            # Keep the main thread alive
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                self.stop()
        
        except Exception as e:
            logger.error(f"Error starting gateway: {str(e)}")
            self.stop()

    def stop(self):
        """Stop the gateway"""
        logger.info("Stopping gateway")
        self.running = False
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()


if __name__ == "__main__":
    gateway = SickGateway()
    gateway.start() 