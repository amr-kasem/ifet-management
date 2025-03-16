# gateway.py
import os
import time
import logging
import json
from abc import ABC, abstractmethod
from typing import Dict, Any, List

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("SensorGateway")


# Interface segregation - separate data source interface
class DataSource(ABC):
    """Interface for data sources that can fetch sensor data"""
    
    @abstractmethod
    def fetch_data(self) -> Dict[str, Any]:
        """Fetch data from source"""
        pass
    
    @abstractmethod
    def get_source_id(self) -> str:
        """Get unique identifier for this data source"""
        pass


# Interface segregation - separate data sink interface
class DataSink(ABC):
    """Interface for destinations where sensor data can be published"""
    
    @abstractmethod
    def publish(self, topic: str, payload: Any) -> bool:
        """Publish data to sink"""
        pass
    
    @abstractmethod
    def connect(self) -> bool:
        """Connect to the sink"""
        pass
    
    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect from the sink"""
        pass


# Single responsibility - REST API client
class RestDataSource(DataSource):
    """Implementation of DataSource that fetches from REST API"""
    
    def __init__(self, source_id: str, url: str, headers: Dict = None, params: Dict = None):
        """
        Initialize REST data source
        
        Args:
            source_id: Unique identifier for this source
            url: REST API URL to fetch data from
            headers: Optional HTTP headers
            params: Optional query parameters
        """
        import requests
        self.requests = requests
        self.source_id = source_id
        self.url = url
        self.headers = headers or {}
        self.params = params or {}
        logger.info(f"Initialized REST data source: {source_id} -> {url}")
    
    def fetch_data(self) -> Dict[str, Any]:
        """Fetch data from REST API"""
        try:
            logger.debug(f"Fetching data from {self.url}")
            response = self.requests.get(
                self.url,
                headers=self.headers,
                params=self.params,
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching data from {self.url}: {e}")
            return {"error": str(e)}
    
    def get_source_id(self) -> str:
        """Get source identifier"""
        return self.source_id


# Single responsibility - MQTT client
class MqttDataSink(DataSink):
    """Implementation of DataSink that publishes to MQTT broker"""
    
    def __init__(self, broker: str, port: int = 1883, client_id: str = None,
                 username: str = None, password: str = None):
        """
        Initialize MQTT data sink
        
        Args:
            broker: MQTT broker address
            port: MQTT broker port
            client_id: Optional client ID
            username: Optional username for authentication
            password: Optional password for authentication
        """
        import paho.mqtt.client as mqtt
        self.mqtt = mqtt
        self.broker = broker
        self.port = port
        self.client_id = client_id or f"sensor-gateway-{os.getpid()}"
        self.username = username
        self.password = password
        self.client = self.mqtt.Client(client_id=self.client_id)
        
        if username and password:
            self.client.username_pw_set(username, password)
        
        self.client.on_connect = self._on_connect
        self.client.on_publish = self._on_publish
        logger.info(f"Initialized MQTT sink: {broker}:{port}")
    
    def _on_connect(self, client, userdata, flags, rc):
        """Callback for MQTT connection"""
        if rc == 0:
            logger.info(f"Connected to MQTT broker at {self.broker}:{self.port}")
        else:
            logger.error(f"Failed to connect to MQTT broker, return code: {rc}")
    
    def _on_publish(self, client, userdata, mid):
        """Callback for MQTT publish"""
        logger.debug(f"Message {mid} published")
    
    def connect(self) -> bool:
        """Connect to MQTT broker"""
        try:
            self.client.connect(self.broker, self.port)
            self.client.loop_start()
            return True
        except Exception as e:
            logger.error(f"Failed to connect to MQTT broker: {e}")
            return False
    
    def disconnect(self) -> None:
        """Disconnect from MQTT broker"""
        self.client.loop_stop()
        self.client.disconnect()
        logger.info("Disconnected from MQTT broker")
    
    def publish(self, topic: str, payload: Any) -> bool:
        """Publish message to MQTT topic"""
        try:
            # Convert payload to JSON string if it's a dict
            if isinstance(payload, dict):
                payload = json.dumps(payload)
            
            result = self.client.publish(topic, payload, qos=1)
            if result.rc == self.mqtt.MQTT_ERR_SUCCESS:
                logger.info(f"Published data to {topic}")
                return True
            else:
                logger.error(f"Failed to publish to MQTT topic: {result.rc}")
                return False
        except Exception as e:
            logger.error(f"Error publishing to MQTT: {e}")
            return False


# Open/closed principle - sensor transformer interface
class SensorTransformer(ABC):
    """Interface for transforming sensor data"""
    
    @abstractmethod
    def transform(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Transform sensor data"""
        pass
    
    @abstractmethod
    def get_topic_suffix(self) -> str:
        """Get topic suffix for this transformer"""
        pass


# Dependency inversion - Gateway uses abstractions
class SensorGateway:
    """Gateway that connects data sources to data sinks via transformers"""
    
    def __init__(self, base_topic: str = "sensors"):
        """
        Initialize sensor gateway
        
        Args:
            base_topic: Base MQTT topic prefix
        """
        self.sources: List[DataSource] = []
        self.transformers: Dict[str, SensorTransformer] = {}
        self.sink: DataSink = None
        self.base_topic = base_topic
        self.running = False
        logger.info(f"Initialized gateway with base topic: {base_topic}")
    
    def add_source(self, source: DataSource) -> None:
        """Add data source to gateway"""
        self.sources.append(source)
        logger.info(f"Added data source: {source.get_source_id()}")
    
    def set_sink(self, sink: DataSink) -> None:
        """Set data sink for gateway"""
        self.sink = sink
        logger.info("Set data sink")
    
    def register_transformer(self, source_id: str, transformer: SensorTransformer) -> None:
        """Register transformer for a specific source"""
        self.transformers[source_id] = transformer
        logger.info(f"Registered {transformer.__class__.__name__} for source: {source_id}")
    
    def start(self, poll_interval: int = 60) -> None:
        """
        Start the gateway
        
        Args:
            poll_interval: Time in seconds between polls
        """
        if not self.sources:
            logger.error("No data sources configured. Cannot start gateway.")
            return
        
        if not self.sink:
            logger.error("No data sink configured. Cannot start gateway.")
            return
        
        if not self.sink.connect():
            logger.error("Failed to connect to data sink. Cannot start gateway.")
            return
        
        self.running = True
        logger.info(f"Starting gateway with poll interval: {poll_interval}s")
        
        try:
            while self.running:
                for source in self.sources:
                    source_id = source.get_source_id()
                    logger.info(f"Polling source: {source_id}")
                    
                    try:
                        # Fetch data from source
                        data = source.fetch_data()
                        
                        # Apply transformation if registered
                        if source_id in self.transformers:
                            transformer = self.transformers[source_id]
                            transformed_data = transformer.transform(data)
                            
                            # Determine topic
                            topic = f"{self.base_topic}/{transformer.get_topic_suffix()}/{source_id}"
                            
                            # Publish to sink
                            self.sink.publish(topic, transformed_data)
                        else:
                            # No transformer, publish raw data
                            logger.warning(f"No transformer for source {source_id}, publishing raw data")
                            topic = f"{self.base_topic}/raw/{source_id}"
                            self.sink.publish(topic, data)
                    
                    except Exception as e:
                        logger.error(f"Error processing source {source_id}: {e}")
                
                time.sleep(poll_interval)
        
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
        finally:
            self.stop()
    
    def stop(self) -> None:
        """Stop the gateway"""
        self.running = False
        if self.sink:
            self.sink.disconnect()
        logger.info("Gateway stopped")

