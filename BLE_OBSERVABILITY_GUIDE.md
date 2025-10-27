# BLE Interface Observability Guide

## Overview

The BLE interface now includes comprehensive observability features that provide detailed insights into connection performance, data transfer metrics, error patterns, and system health. This guide explains how to use these features for monitoring, debugging, and optimizing BLE operations.

## Features

### 📊 **Metrics Collection**
- Connection statistics (attempts, successes, failures, success rates)
- Data transfer metrics (bytes sent/received, packet counts, average sizes)
- Notification tracking (total received, malformed count)
- Operation retry and timeout counters
- Error and warning aggregation

### 📈 **Performance Monitoring**
- Operation timing with automatic slow operation detection
- Performance statistics (min, max, average, 95th percentile)
- Connection duration tracking
- Historical performance samples

### 🔍 **Health Assessment**
- Real-time health scoring (0-100)
- Automatic issue detection and classification
- Health status indicators (healthy, degraded, unhealthy)
- Root cause analysis hints

### 📝 **Error Tracking**
- Comprehensive error logging with context
- Error history with timestamps
- Warning aggregation and tracking
- Malformed notification monitoring

## Usage

### Basic Metrics Access

```python
from meshtastic import BLEInterface

# Create BLE interface
interface = BLEInterface(address=None)

# Get current metrics
metrics = interface.get_observability_metrics()
print(f"Connection success rate: {metrics['connection_success_rate']:.2%}")
print(f"Average packet size sent: {metrics['avg_packet_size_sent']:.0f} bytes")
print(f"Error count: {metrics['error_count']}")
```

### Performance Analysis

```python
# Get performance summary
perf = interface.get_performance_summary()
for operation, stats in perf.items():
    print(f"{operation}: avg={stats['avg']:.3f}s, p95={stats['p95']:.3f}s")
```

### Health Monitoring

```python
# Check system health
health = interface.get_health_status()
print(f"Status: {health['status']}")
print(f"Score: {health['score']}/100")

if health['issues']:
    print("Issues detected:")
    for issue in health['issues']:
        print(f"  - {issue}")
```

### Diagnostic Export

```python
# Export comprehensive diagnostics
diagnostics = interface.export_diagnostics()

# Save to file for analysis
import json
with open('ble_diagnostics.json', 'w') as f:
    json.dump(diagnostics, f, indent=2, default=str)
```

### Connection History

```python
# Get recent connection attempts
history = interface.get_connection_history(count=10)
for attempt in history:
    print(f"{attempt['timestamp']}: {attempt['status']} to {attempt['address']}")
```

### Error Analysis

```python
# Get recent errors
errors = interface.get_recent_errors(count=20)
for error in errors:
    print(f"{error['timestamp']}: {error['type']} - {error['message']}")
```

## Metrics Reference

### Connection Metrics
- `connection_attempts`: Total connection attempts
- `connection_successes`: Successful connections
- `connection_failures`: Failed connections
- `connection_success_rate`: Success rate (0.0-1.0)
- `reconnection_attempts`: Auto-reconnect attempts
- `reconnection_successes`: Successful reconnections
- `reconnection_success_rate`: Reconnect success rate

### Data Transfer Metrics
- `bytes_sent`: Total bytes transmitted
- `bytes_received`: Total bytes received
- `packets_sent`: Total packets transmitted
- `packets_received`: Total packets received
- `avg_packet_size_sent`: Average outbound packet size
- `avg_packet_size_received`: Average inbound packet size

### Notification Metrics
- `notifications_received`: Total notifications processed
- `malformed_notifications`: Malformed notification count

### Operation Metrics
- `read_retries`: Read operation retries
- `read_timeouts`: Read operation timeouts
- `write_retries`: Write operation retries
- `write_timeouts`: Write operation timeouts

### Error Metrics
- `error_count`: Total errors encountered
- `warning_count`: Total warnings encountered

## Health Scoring

The health score (0-100) is calculated based on:

### Error Rate (Weight: 20%)
- >10 errors in 5 minutes: -20 points
- 5-10 errors in 5 minutes: -10 points

### Connection Success Rate (Weight: 30%)
- <50% success rate: -30 points
- 50-80% success rate: -15 points

### Malformed Notification Rate (Weight: 25%)
- >10% malformed: -25 points
- 5-10% malformed: -10 points

### Health Status Classification
- **80-100**: Healthy - Normal operation
- **60-79**: Degraded - Some issues but functional
- **0-59**: Unhealthy - Significant problems

## Performance Thresholds

### Slow Operation Detection
- Default threshold: 5.0 seconds
- Automatically logs warnings for slow operations
- Configurable via `BLEObservability._slow_operation_threshold`

### Connection Timeouts
- Connection timeout: 60 seconds
- GATT I/O timeout: 30 seconds
- Notification start timeout: 10 seconds

## Integration with Existing Code

The observability system is designed to be non-intrusive:

1. **Zero Impact**: All observability features are optional and can be disabled
2. **Thread-Safe**: All metrics collection is thread-safe
3. **Low Overhead**: Minimal performance impact on BLE operations
4. **Backward Compatible**: No changes to existing API

## Debugging Scenarios

### Connection Issues
```python
# Check connection history
history = interface.get_connection_history()
if history and history[-1]['status'] == 'failed':
    print(f"Last connection failed: {history[-1]['error']}")

# Check error patterns
errors = interface.get_recent_errors()
connection_errors = [e for e in errors if 'connection' in e['type'].lower()]
```

### Performance Issues
```python
# Check for slow operations
perf = interface.get_performance_summary()
for op, stats in perf.items():
    if stats['p95'] > 2.0:  # 95th percentile > 2 seconds
        print(f"Slow operation detected: {op} (p95: {stats['p95']:.3f}s)")
```

### Data Quality Issues
```python
# Check malformed notification rate
metrics = interface.get_observability_metrics()
if metrics['notifications_received'] > 0:
    malformed_rate = metrics['malformed_notifications'] / metrics['notifications_received']
    if malformed_rate > 0.05:  # >5% malformed
        print(f"High malformed notification rate: {malformed_rate:.2%}")
```

## Best Practices

### 1. Regular Health Monitoring
```python
def monitor_ble_health(interface):
    health = interface.get_health_status()
    if health['status'] != 'healthy':
        print(f"BLE health issue: {health['status']} (score: {health['score']})")
        for issue in health['issues']:
            print(f"  - {issue}")
```

### 2. Performance Trend Analysis
```python
def analyze_performance_trends(interface):
    perf = interface.get_performance_summary()
    for operation, stats in perf.items():
        if stats['count'] > 10:  # Sufficient sample size
            if stats['avg'] > stats['p95'] * 0.5:
                print(f"{operation}: Performance variance detected")
```

### 3. Proactive Error Detection
```python
def check_error_patterns(interface):
    errors = interface.get_recent_errors(count=50)
    error_types = {}
    for error in errors:
        error_type = error['type']
        error_types[error_type] = error_types.get(error_type, 0) + 1
    
    for error_type, count in error_types.items():
        if count > 5:  # More than 5 of same error type
            print(f"Frequent error pattern: {error_type} ({count} occurrences)")
```

## Configuration

### Enabling/Disabling Observability
```python
# Observability is enabled by default
interface = BLEInterface(address=None)

# To disable (not recommended for production)
interface._observability.enabled = False
```

### Resetting Metrics
```python
# Reset all metrics and history
interface.reset_observability_metrics()
```

### Custom Thresholds
```python
# Adjust slow operation threshold
interface._observability._slow_operation_threshold = 3.0  # 3 seconds
```

## Troubleshooting

### Common Issues

1. **High Memory Usage**: Reset metrics periodically if running long-term
2. **Missing Data**: Ensure observability is enabled
3. **Performance Impact**: Check if slow operation threshold is too low

### Debug Information
```python
# Export full diagnostics for support
diagnostics = interface.export_diagnostics()
print(f"System info: {diagnostics['system_info']}")
print(f"Interface info: {diagnostics['interface_info']}")
```

## Integration with Monitoring Systems

The observability data can be easily integrated with external monitoring systems:

### Prometheus Integration
```python
from prometheus_client import Gauge, Counter

# Create metrics
connection_success_rate = Gauge('ble_connection_success_rate', 'BLE connection success rate')
bytes_transferred = Counter('ble_bytes_transferred_total', 'Total bytes transferred', ['direction'])

# Update metrics
metrics = interface.get_observability_metrics()
connection_success_rate.set(metrics['connection_success_rate'])
bytes_transferred.labels(direction='sent').inc(metrics['bytes_sent'])
bytes_transferred.labels(direction='received').inc(metrics['bytes_received'])
```

### Grafana Dashboards
Use the exported diagnostics to create comprehensive dashboards showing:
- Connection success rates over time
- Data transfer volumes
- Error rates and patterns
- Performance metrics
- Health status trends

## Conclusion

The BLE observability system provides comprehensive monitoring capabilities that help ensure reliable operation, quickly identify issues, and optimize performance. Regular monitoring of these metrics will help maintain high-quality BLE connections and provide valuable insights for troubleshooting and optimization.