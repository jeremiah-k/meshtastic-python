"""
Meshtastic Client Proxy Management

This module provides high-level APIs for managing MQTT and UDP client proxy functionality.
It allows applications to easily set up proxy connections through Meshtastic devices.
"""

import logging
import socket
import threading
import time
from typing import Callable, Optional, Union
from pypubsub import pub


class ProxyManager:
    """Manages MQTT and UDP proxy connections through a Meshtastic interface."""
    
    def __init__(self, interface):
        """Initialize the proxy manager.
        
        Args:
            interface: MeshInterface instance to use for proxy communication
        """
        self.interface = interface
        self.mqtt_handlers = {}
        self.udp_handlers = {}
        self.udp_sockets = {}
        self._running = False
        
        # Subscribe to proxy messages
        pub.subscribe(self._handle_mqtt_proxy_message, "meshtastic.mqttclientproxymessage")
        pub.subscribe(self._handle_udp_proxy_message, "meshtastic.udpclientproxymessage")
    
    def start_mqtt_proxy(self, topic: str, callback: Callable[[str, Union[bytes, str], bool], None]):
        """Start proxying MQTT messages for a specific topic.
        
        Args:
            topic: MQTT topic to proxy
            callback: Function to call when messages are received
                     Signature: callback(topic, payload, retained)
        """
        self.mqtt_handlers[topic] = callback
        logging.info(f"Started MQTT proxy for topic: {topic}")
    
    def start_udp_proxy(self, host: str, port: int, callback: Callable[[str, int, Union[bytes, str]], None]):
        """Start proxying UDP messages for a specific host:port.
        
        Args:
            host: UDP host to proxy
            port: UDP port to proxy
            callback: Function to call when messages are received
                     Signature: callback(host, port, payload)
        """
        key = f"{host}:{port}"
        self.udp_handlers[key] = callback
        logging.info(f"Started UDP proxy for {host}:{port}")
    
    def stop_mqtt_proxy(self, topic: str):
        """Stop proxying MQTT messages for a specific topic.
        
        Args:
            topic: MQTT topic to stop proxying
        """
        if topic in self.mqtt_handlers:
            del self.mqtt_handlers[topic]
            logging.info(f"Stopped MQTT proxy for topic: {topic}")
    
    def stop_udp_proxy(self, host: str, port: int):
        """Stop proxying UDP messages for a specific host:port.
        
        Args:
            host: UDP host to stop proxying
            port: UDP port to stop proxying
        """
        key = f"{host}:{port}"
        if key in self.udp_handlers:
            del self.udp_handlers[key]
            logging.info(f"Stopped UDP proxy for {host}:{port}")
    
    def send_mqtt_message(self, topic: str, payload: Union[bytes, str], retained: bool = False):
        """Send an MQTT message through the proxy.
        
        Args:
            topic: MQTT topic to publish to
            payload: Message payload (bytes or string)
            retained: Whether the message should be retained
        """
        if isinstance(payload, str):
            self.interface.sendMqttClientProxyMessage(topic=topic, text=payload, retained=retained)
        else:
            self.interface.sendMqttClientProxyMessage(topic=topic, data=payload, retained=retained)
    
    def send_udp_message(self, host: str, port: int, payload: Union[bytes, str]):
        """Send a UDP message through the proxy.
        
        Args:
            host: UDP destination host
            port: UDP destination port
            payload: Message payload (bytes or string)
        """
        if isinstance(payload, str):
            self.interface.sendUdpClientProxyMessage(host=host, port=port, text=payload)
        else:
            self.interface.sendUdpClientProxyMessage(host=host, port=port, data=payload)
    
    def _handle_mqtt_proxy_message(self, proxymessage, interface):
        """Handle incoming MQTT proxy messages from the device."""
        if interface != self.interface:
            return
            
        topic = proxymessage.topic
        retained = proxymessage.retained
        
        # Get payload (either data or text)
        if proxymessage.HasField("data"):
            payload = proxymessage.data
        else:
            payload = proxymessage.text
        
        # Call registered handler if available
        if topic in self.mqtt_handlers:
            try:
                self.mqtt_handlers[topic](topic, payload, retained)
            except Exception as e:
                logging.error(f"Error in MQTT proxy handler for topic {topic}: {e}")
        else:
            logging.debug(f"No handler registered for MQTT topic: {topic}")
    
    def _handle_udp_proxy_message(self, proxymessage, interface):
        """Handle incoming UDP proxy messages from the device."""
        if interface != self.interface:
            return
            
        # Parse UDP destination from topic (format: "udp:host:port")
        topic = proxymessage.topic
        if not topic.startswith("udp:"):
            logging.warning(f"Invalid UDP proxy topic format: {topic}")
            return
            
        try:
            _, host, port_str = topic.split(":", 2)
            port = int(port_str)
        except ValueError:
            logging.error(f"Failed to parse UDP destination from topic: {topic}")
            return
        
        # Get payload (either data or text)
        if proxymessage.HasField("data"):
            payload = proxymessage.data
        else:
            payload = proxymessage.text
        
        # Call registered handler if available
        key = f"{host}:{port}"
        if key in self.udp_handlers:
            try:
                self.udp_handlers[key](host, port, payload)
            except Exception as e:
                logging.error(f"Error in UDP proxy handler for {host}:{port}: {e}")
        else:
            logging.debug(f"No handler registered for UDP destination: {host}:{port}")
    
    def get_mqtt_topics(self):
        """Get list of currently proxied MQTT topics."""
        return list(self.mqtt_handlers.keys())
    
    def get_udp_destinations(self):
        """Get list of currently proxied UDP destinations."""
        return [dest.split(":") for dest in self.udp_handlers.keys()]
    
    def stop_all_proxies(self):
        """Stop all proxy handlers."""
        self.mqtt_handlers.clear()
        self.udp_handlers.clear()
        logging.info("Stopped all proxy handlers")


def create_proxy_manager(interface) -> ProxyManager:
    """Create a new proxy manager instance.
    
    Args:
        interface: MeshInterface instance to use for proxy communication
        
    Returns:
        ProxyManager instance
    """
    return ProxyManager(interface)
