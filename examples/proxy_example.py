#!/usr/bin/env python3
"""
Meshtastic Client Proxy Example

This example demonstrates how to use the MQTT and UDP client proxy functionality
to route messages through a Meshtastic device.
"""

import time
import logging
import meshtastic
import meshtastic.serial_interface
from meshtastic.proxy import create_proxy_manager

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def mqtt_message_handler(topic, payload, retained):
    """Handle incoming MQTT messages from the mesh."""
    logger.info(f"Received MQTT message:")
    logger.info(f"  Topic: {topic}")
    logger.info(f"  Payload: {payload}")
    logger.info(f"  Retained: {retained}")


def udp_message_handler(host, port, payload):
    """Handle incoming UDP messages from the mesh."""
    logger.info(f"Received UDP message:")
    logger.info(f"  Destination: {host}:{port}")
    logger.info(f"  Payload: {payload}")


def main():
    """Main example function."""
    logger.info("Starting Meshtastic Client Proxy Example")
    
    # Connect to Meshtastic device
    # You can also use meshtastic.ble_interface.BLEInterface() or meshtastic.tcp_interface.TCPInterface()
    try:
        interface = meshtastic.serial_interface.SerialInterface()
        logger.info("Connected to Meshtastic device via serial")
    except Exception as e:
        logger.error(f"Failed to connect to Meshtastic device: {e}")
        return
    
    # Create proxy manager
    proxy = create_proxy_manager(interface)
    
    # Set up MQTT proxy for a specific topic
    mqtt_topic = "meshtastic/test"
    proxy.start_mqtt_proxy(mqtt_topic, mqtt_message_handler)
    logger.info(f"Started MQTT proxy for topic: {mqtt_topic}")
    
    # Set up UDP proxy for a specific destination
    udp_host = "192.168.1.100"
    udp_port = 1234
    proxy.start_udp_proxy(udp_host, udp_port, udp_message_handler)
    logger.info(f"Started UDP proxy for {udp_host}:{udp_port}")
    
    try:
        # Send some test messages
        logger.info("Sending test messages...")
        
        # Send MQTT message
        proxy.send_mqtt_message(mqtt_topic, "Hello from MQTT proxy!", retained=False)
        logger.info("Sent MQTT test message")
        
        # Send UDP message
        proxy.send_udp_message(udp_host, udp_port, "Hello from UDP proxy!")
        logger.info("Sent UDP test message")
        
        # Keep the example running to receive messages
        logger.info("Proxy is running. Press Ctrl+C to stop.")
        logger.info("Current MQTT topics: " + str(proxy.get_mqtt_topics()))
        logger.info("Current UDP destinations: " + str(proxy.get_udp_destinations()))
        
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("Stopping proxy example...")
    finally:
        # Clean up
        proxy.stop_all_proxies()
        interface.close()
        logger.info("Proxy example stopped")


if __name__ == "__main__":
    main()
