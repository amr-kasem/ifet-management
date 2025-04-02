from fastapi import FastAPI
import random
import time

app = FastAPI()

# Store for device data to simulate consistent readings
device_data = {}

def generate_process_data(master_id, port_id):
    device_key = f"{master_id}{port_id}"
    
    # If we've never seen this device before, create random initial values
    if device_key not in device_data:
        first_value = random.randint(0, 1)
        second_value = random.randint(0, 255)
        device_data[device_key] = [first_value, second_value, 0, 69]  # Simulated process values
    
    # Otherwise, slightly modify the existing values to simulate changes
    else:
        current_values = device_data[device_key]
        # Only change the second value (measurement value) slightly
        device_data[device_key][1] = max(0, min(255, current_values[1] + random.randint(-5, 5)))
    
    return {
        "iolink": {
            "valid": True,
            "value": device_data[device_key].copy(),
            "timestamp": time.time()
        }
    }

@app.get("/iolink/v1/devices/{master_id}{port_id}/processdata/getdata/value")
def get_process_data(master_id: str, port_id: str):
    return generate_process_data(master_id, port_id)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3197)
