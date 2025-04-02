# SICK MQTT Gateway

The SICK MQTT Gateway serves as a bridge between SICK sensors and MQTT, enabling integration with the IFET system. This gateway provides the following features:

- **Sensor Data Retrieval**: Periodically polls SICK sensors for data and publishes it to MQTT.
- **Testing System Integration**: Supports assigning sensors to testing systems with automatic zeroing and tracking maximum deflection values.
- **Unit Conversion**: Configurable unit of measurement (millimeters or inches) for sensor readings.
- **Error Handling**: Robust error handling and logging for reliable operation.

## Device Configuration

The gateway can be configured using multiple methods, with the following priority:

1. Configuration file (recommended)
2. Environment variables
3. Default values

### Configuration File

The recommended method is to use a configuration file (`config.json`) with the following structure:

```json
{
  "devices": [
    {
      "id": "device1",
      "master_id": "1",
      "port_id": "1"
    },
    {
      "id": "device2",
      "master_id": "1",
      "port_id": "2"
    }
  ],
  "mqtt_topic_prefix": "sick"
}
```

### Environment Variables

Alternatively, you can configure the gateway using environment variables:

- `SICK_API_HOST`: Hostname/IP of the SICK API (default: "sick_api_simulator")
- `SICK_API_PORT`: Port of the SICK API (default: 3197)
- `MQTT_BROKER`: MQTT broker hostname/IP (default: "mosquitto")
- `MQTT_PORT`: MQTT broker port (default: 1883)
- `POLL_INTERVAL`: Polling interval in seconds (default: 1)
- `CONFIG_FILE`: Path to the configuration file (default: "config.json")
- `SICK_DEVICES`: Comma-separated list of devices in the format "device_id:master_id:port_id"
- `LOG_LEVEL`: Logging level (default: "INFO")
- `UNIT_OF_MEASUREMENT`: Unit for sensor readings - "mm" or "inch" (default: "mm")

### .env File Support

The gateway also supports loading environment variables from a `.env` file located in the same directory as the gateway script. This is useful for local development and testing.

## Testing System Features

The gateway supports assigning sensors to testing systems and tracking maximum deflection values during tests.

### Assigning a Sensor

To assign a sensor to a testing system, publish a message to the topic `sick/assign/{device_id}` with the following payload:

```json
{
  "testing_system_id": "test_system_123"
}
```

When a sensor is assigned:
1. The sensor is automatically zeroed (current reading becomes the reference point)
2. Maximum value tracking begins from this zero point
3. The assignment status is published, including the zero offset

### Releasing a Sensor

To release a sensor from a testing system, publish a message to the topic `sick/release/{device_id}` (the payload is ignored).

Upon release:
1. The maximum value observed during the assignment is reported
2. The sensor is unassigned
3. The zero reference is reset
4. The release status is published

## Automatic Zeroing

Zeroing is integrated into the assignment process and happens automatically when a sensor is assigned to a testing system.

1. When a sensor is assigned, its current reading is stored as the zero reference point
2. All subsequent readings are adjusted relative to this reference
3. When the sensor is released, the zero reference is cleared

Note: The standalone zero commands are no longer supported. Zeroing is now exclusively handled as part of the assignment process.

## Unit Conversion

The gateway supports displaying sensor readings in either millimeters (mm) or inches.

### Configuration

Set the unit of measurement using the `UNIT_OF_MEASUREMENT` environment variable or in your `.env` file:

```
UNIT_OF_MEASUREMENT=mm  # For millimeters (default)
```

or 

```
UNIT_OF_MEASUREMENT=inch  # For inches
```

### Sensor Data Format

The sensor data published to MQTT includes the unit of measurement:

```json
{
  "valid": true,
  "raw_value": 25.4,
  "value": 0,
  "offset": 25.4,
  "zeroed": true,
  "assigned_to": "test_system_123",
  "max_value": 10.5,
  "units": "mm",
  "timestamp": 1623456789.123
}
```

All numeric values (raw_value, value, offset, max_value) are presented in the configured unit of measurement.

## Testing

### Test Script

The gateway includes a test script (`test_testing_system.py`) to demonstrate the testing system features. Run it with:

```bash
python test_testing_system.py --broker localhost --device device1 --action simulate
```

### Manual Testing

You can also test the features using `mosquitto_pub` and `mosquitto_sub`:

```bash
# Assign a sensor to a testing system (which automatically zeros it)
mosquitto_pub -h localhost -t sick/assign/device1 -m '{"testing_system_id": "test_system_123"}'

# Check assignment status (includes zeroing information)
mosquitto_sub -h localhost -t sick/devices/device1/assignment_status

# Check sensor data
mosquitto_sub -h localhost -t sick/devices/device1

# Release a sensor
mosquitto_pub -h localhost -t sick/release/device1 -m '{}'
```

## Integration with IFET

The SICK MQTT Gateway integrates with the IFET system to provide enhanced capabilities for pressure and position measurements, particularly for testing systems measuring deflections.

## Troubleshooting

If you encounter issues with the gateway:

1. Check the logs for error messages
2. Verify that the SICK API and MQTT broker are accessible
3. Confirm that your device configuration is correct
4. Ensure the unit of measurement setting is valid ("mm" or "inch")

## MQTT Topics

### Sensor Data

Sensor data is published to:
```
sick/devices/{device_id}
```

The payload is a JSON object containing:
```json
{
  "valid": true,
  "raw_value": 123,     // Original value from the sensor
  "value": 120,         // Value after applying zero offset
  "offset": 3,          // Current zero offset
  "zeroed": true,       // Whether zero has been applied
  "assigned_to": "test_system_123", // Current assignment (null if not assigned)
  "max_value": 140,     // Maximum value since assignment (null if not assigned)
  "units": "mm",        // Unit of measurement
  "timestamp": 1679305245.123
}
```

### Assignment Status

When a sensor is assigned or released, status information is published to:
```
sick/devices/{device_id}/assignment_status
```

The payload for an assignment:
```json
{
  "assigned_to": "test_system_123",
  "assigned_at": 1679305245.123,
  "zeroed": true,
  "offset": 3
}
```

The payload for a release:
```json
{
  "assigned_to": null,
  "released_at": 1679305345.456,
  "previous_assignment": "test_system_123",
  "max_value": 15.2,
  "zeroed": false
}
```

### Assignment Commands

To assign a device to a testing system (which also zeroes it):
```
sick/assign/{device_id}
```

With payload:
```json
{
  "testing_system_id": "test_system_123"
}
```

To release a device from a testing system:
```
sick/release/{device_id}
```

With any payload (content is ignored).

## Configuration

The gateway is configured using environment variables:

- `SICK_API_HOST`: Hostname of the SICK API (default: "sick_api_simulator")
- `SICK_API_PORT`: Port of the SICK API (default: 3197)
- `MQTT_BROKER`: MQTT broker hostname (default: "mosquitto")
- `MQTT_PORT`: MQTT broker port (default: 1883)
- `POLL_INTERVAL`: Polling interval in seconds (default: 1)
- `CONFIG_FILE`: Path to the configuration file (default: "config.json")
- `SICK_DEVICES`: Comma-separated list of devices (format: "device_id:master_id:port_id")
- `LOG_LEVEL`: Logging level (default: "INFO", options: "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

These can be set directly as environment variables or in a `.env` file.

## Docker Deployment

The service can be deployed using Docker Compose. See the top-level compose.yaml file for configuration.

Example Docker Compose configuration:

```yaml
sick_gateway:
  build: ./src/sick_gateway/
  volumes:
    - ./src/sick_gateway/sick_mqtt_gateway.py:/app/sick_mqtt_gateway.py
    - ./src/sick_gateway/config.json:/app/config.json
    - ./src/sick_gateway/.env:/app/.env
  env_file:
    - ./src/sick_gateway/.env
  depends_on:
    - mosquitto
    - sick_api_simulator
  environment:
    # These override any values from .env file
    SICK_API_HOST: sick_api_simulator
    MQTT_BROKER: mosquitto
  restart: always
```

## API Simulator

The SICK API simulator provides endpoints that mimic the behavior of real SICK sensors:

- `GET /iolink/v1/devices/{master_id}{port_id}/processdata/getdata/value` - Get sensor readings

Unlike the gateway, the SICK API simulator intentionally does not implement zero capability, to simulate real SICK sensors.

## Integration with IFET

This gateway is designed to work with the IFET system, allowing SICK sensors to be used for pressure/position measurements with the added zero reference capability. The new testing system integration features make it ideal for tracking deflections during tests.

## Troubleshooting

If you encounter issues with the gateway:

1. Check that the SICK API is accessible
2. Verify MQTT broker connectivity
3. Check for error messages in the gateway logs
4. Ensure the device IDs are configured correctly
5. Verify the configuration file format if using one
6. Check the environment variables if not using a configuration file
7. Set `LOG_LEVEL=DEBUG` for more detailed logs

For more help, consult the IFET documentation or contact system support. 