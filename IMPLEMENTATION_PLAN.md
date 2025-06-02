# UDP and MQTT Client Proxy Implementation Plan

## Overview
Implementing UDP Client Proxy functionality for Meshtastic-Python to complement the existing MQTT Client Proxy, enabling projects like meshtastic-matrix-relay (mmrelay) to use UDP communication through the python library.

## Current State Analysis

### Existing MQTT Proxy Implementation
- **Protobuf**: `MqttClientProxyMessage` in mesh.proto with fields:
  - `string topic = 1`
  - `oneof payload_variant { bytes data = 2; string text = 3; }`
  - `bool retained = 4`
- **Handler**: mesh_interface.py lines 1315-1321 with `pub.sendMessage("meshtastic.mqttclientproxymessage")`
- **Bidirectional**: Both FromRadio and ToRadio support mqttClientProxyMessage field

### Target Integration
- Enable UDP proxy functionality similar to MQTT proxy
- Support both UDP and MQTT proxying simultaneously
- Maintain backward compatibility

## Implementation Strategy

### Phase 1: Protobuf Extension (Current Task)
1. **Analyze if MqttClientProxyMessage can be reused for UDP**
   - Topic field could be repurposed as "destination" (host:port)
   - Data/text payload remains the same
   - Retained field could be ignored for UDP or repurposed

2. **Decision**: Reuse existing MqttClientProxyMessage vs create new UdpClientProxyMessage
   - **Option A**: Reuse existing message type with convention-based routing
   - **Option B**: Create separate UdpClientProxyMessage type

### Phase 2: Core Implementation
1. **mesh_interface.py**: Add UDP proxy message handling
2. **High-level API**: Create proxy management functions
3. **Connection interfaces**: Add proxy parameters to connection methods

### Phase 3: Testing and Integration
1. **Unit tests**: Test UDP proxy message routing
2. **Integration tests**: Test with actual devices
3. **MMRelay compatibility**: Ensure seamless integration

## Implementation Status
1. Create branch and initial commit ✅
2. Analyze protobuf reuse vs new message type ✅
3. Implement UDP proxy message handling ✅
4. Create high-level proxy API ✅
5. Test and validate implementation ✅

## Implementation Details

### Decision: Reuse MqttClientProxyMessage
- **Rationale**: Avoids protobuf changes, simpler implementation, maintains compatibility
- **Convention**: Use topic prefix "udp:host:port" for UDP messages
- **Backward Compatibility**: MQTT messages without "udp:" prefix work as before

### Key Components Implemented
1. **mesh_interface.py**: Enhanced message routing with UDP/MQTT detection
2. **proxy.py**: High-level ProxyManager class for easy proxy management
3. **examples/proxy_example.py**: Complete usage demonstration
4. **tests/test_proxy.py**: Comprehensive test coverage

### API Usage
```python
import meshtastic.serial_interface
from meshtastic.proxy import create_proxy_manager

# Connect and create proxy manager
interface = meshtastic.serial_interface.SerialInterface()
proxy = create_proxy_manager(interface)

# Set up handlers
proxy.start_mqtt_proxy("topic", mqtt_handler)
proxy.start_udp_proxy("192.168.1.100", 1234, udp_handler)

# Send messages
proxy.send_mqtt_message("topic", "Hello MQTT!")
proxy.send_udp_message("192.168.1.100", 1234, "Hello UDP!")
```

## Files to Modify
- `meshtastic/mesh_interface.py` - Add UDP proxy handling
- `meshtastic/protobuf/mesh_pb2.py` - Protobuf definitions (if needed)
- New: `meshtastic/proxy.py` - High-level proxy management API
- Tests: `meshtastic/tests/test_proxy.py`
- Examples: `examples/proxy_example.py`

## Success Criteria
- UDP proxy messages route between mesh and UDP destinations
- MQTT proxy functionality maintained and enhanced
- Backward compatibility preserved
- Clear API for proxy management
- Comprehensive test coverage
