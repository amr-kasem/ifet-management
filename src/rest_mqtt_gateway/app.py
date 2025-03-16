# gateway.py

import os
from dotenv import load_dotenv
from gateway import SensorGateway, RestDataSource, MqttDataSink
from sensor_transformers import ProximitySensorTransformer
import logging

logger = logging.getLogger(__name__)

# Config factory - follows factory pattern
class GatewayConfigFactory:
    """Factory for creating gateway configuration from environment variables"""
    
    @staticmethod
    def create_from_env() -> SensorGateway:
        """Create gateway from environment variables"""
        load_dotenv()
        
        # Create gateway with base topic
        gateway = SensorGateway(
            base_topic=os.getenv("MQTT_BASE_TOPIC", "sensors")
        )
        
        # Create and configure data sink
        sink = MqttDataSink(
            broker=os.getenv("MQTT_BROKER", "localhost"),
            port=int(os.getenv("MQTT_PORT", 1883)),
            client_id=os.getenv("MQTT_CLIENT_ID"),
            username=os.getenv("MQTT_USERNAME"),
            password=os.getenv("MQTT_PASSWORD")
        )
        gateway.set_sink(sink)
        
        # Find all REST source environment variables
        # Format: REST_SOURCE_<id>_URL, REST_SOURCE_<id>_TYPE
        source_ids = set()
        for key in os.environ:
            if key.startswith("REST_SOURCE_") and key.endswith("_URL"):
                source_id = key[12:-4]  # Extract ID from REST_SOURCE_<id>_URL
                source_ids.add(source_id)
        
        # Create sources and transformers
        for source_id in source_ids:
            url = os.getenv(f"REST_SOURCE_{source_id}_URL")
            if not url:
                continue
            
            # Create headers from REST_SOURCE_<id>_HEADER_<name>
            headers = {}
            for key, value in os.environ.items():
                header_prefix = f"REST_SOURCE_{source_id}_HEADER_"
                if key.startswith(header_prefix):
                    header_name = key[len(header_prefix):]
                    headers[header_name] = value
            
            # Create params from REST_SOURCE_<id>_PARAM_<name>
            params = {}
            for key, value in os.environ.items():
                param_prefix = f"REST_SOURCE_{source_id}_PARAM_"
                if key.startswith(param_prefix):
                    param_name = key[len(param_prefix):]
                    params[param_name] = value
            
            # Create REST data source
            source = RestDataSource(
                source_id=source_id,
                url=url,
                headers=headers if headers else None,
                params=params if params else None
            )
            gateway.add_source(source)
            
            # Create transformer based on type
            sensor_type = os.getenv(f"REST_SOURCE_{source_id}_TYPE", "").lower()
            if sensor_type == "proximity":
                transformer = ProximitySensorTransformer()
                gateway.register_transformer(source_id, transformer)
            else:
                logger.warning(f"Unknown sensor type '{sensor_type}' for source {source_id}")
        
        return gateway


# Main script entry point
def main():
    """Main entry point"""
    try:
        # Create gateway from environment configuration
        gateway = GatewayConfigFactory.create_from_env()
        
        # Start polling
        poll_interval = int(os.getenv("POLL_INTERVAL", 60))
        gateway.start(poll_interval)
    
    except Exception as e:
        logger.error(f"Error in main: {e}")
        return 1
    
    return 0


if __name__ == "__main__":
    exit(main())