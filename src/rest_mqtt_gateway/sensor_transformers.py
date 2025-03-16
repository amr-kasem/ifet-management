import time
from typing import Dict, Any
from gateway import SensorTransformer
import logging

logger = logging.getLogger(__name__)

# Example transformer for temperature sensors
class ProximitySensorTransformer(SensorTransformer):
    """Transform proximity sensor data"""
    
    def __init__(self, unit_conversion: str = None):
        """
        Initialize proximity transformer
        """
    
    def transform(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Transform proximity data"""
        result = {
            "sensor_type": "proximity",
            "timestamp": data.get("timestamp", time.time())
        }
        
        # Extract temperature value with fallbacks for different field names
        proximity = data.get("proximity", 
               data.get("proximity", 
               data.get("value", None)))
        
        if proximity is not None:           
            result["value"] = proximity
            
            # Add additional metadata if available
            if "location" in data:
                result["location"] = data["location"]
            if "device_id" in data:
                result["device_id"] = data["device_id"]
            
            return result
        else:
            logger.warning(f"No proximity value found in data: {data}")
            return {"error": "No proximity value found", "raw_data": data}
    
    def get_topic_suffix(self) -> str:
        """Get topic suffix"""
        return "proximity"

