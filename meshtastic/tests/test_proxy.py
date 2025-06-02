"""Tests for the proxy module."""

import unittest
from unittest.mock import Mock, patch, MagicMock
from meshtastic.proxy import ProxyManager, create_proxy_manager
from meshtastic.protobuf import mesh_pb2
from meshtastic.mesh_interface import MeshInterface


class TestProxyManager(unittest.TestCase):
    """Test cases for ProxyManager class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.mock_interface = Mock()
        self.proxy_manager = ProxyManager(self.mock_interface)
    
    def test_create_proxy_manager(self):
        """Test proxy manager creation."""
        manager = create_proxy_manager(self.mock_interface)
        self.assertIsInstance(manager, ProxyManager)
        self.assertEqual(manager.interface, self.mock_interface)
    
    def test_start_mqtt_proxy(self):
        """Test starting MQTT proxy."""
        topic = "test/topic"
        callback = Mock()
        
        self.proxy_manager.start_mqtt_proxy(topic, callback)
        
        self.assertIn(topic, self.proxy_manager.mqtt_handlers)
        self.assertEqual(self.proxy_manager.mqtt_handlers[topic], callback)
    
    def test_start_udp_proxy(self):
        """Test starting UDP proxy."""
        host = "192.168.1.100"
        port = 1234
        callback = Mock()
        
        self.proxy_manager.start_udp_proxy(host, port, callback)
        
        key = f"{host}:{port}"
        self.assertIn(key, self.proxy_manager.udp_handlers)
        self.assertEqual(self.proxy_manager.udp_handlers[key], callback)
    
    def test_stop_mqtt_proxy(self):
        """Test stopping MQTT proxy."""
        topic = "test/topic"
        callback = Mock()
        
        # Start then stop
        self.proxy_manager.start_mqtt_proxy(topic, callback)
        self.assertIn(topic, self.proxy_manager.mqtt_handlers)
        
        self.proxy_manager.stop_mqtt_proxy(topic)
        self.assertNotIn(topic, self.proxy_manager.mqtt_handlers)
    
    def test_stop_udp_proxy(self):
        """Test stopping UDP proxy."""
        host = "192.168.1.100"
        port = 1234
        callback = Mock()
        
        # Start then stop
        self.proxy_manager.start_udp_proxy(host, port, callback)
        key = f"{host}:{port}"
        self.assertIn(key, self.proxy_manager.udp_handlers)
        
        self.proxy_manager.stop_udp_proxy(host, port)
        self.assertNotIn(key, self.proxy_manager.udp_handlers)
    
    def test_send_mqtt_message_text(self):
        """Test sending MQTT text message."""
        topic = "test/topic"
        payload = "Hello, MQTT!"
        retained = True
        
        self.proxy_manager.send_mqtt_message(topic, payload, retained)
        
        self.mock_interface.sendMqttClientProxyMessage.assert_called_once_with(
            topic=topic, text=payload, retained=retained
        )
    
    def test_send_mqtt_message_data(self):
        """Test sending MQTT binary message."""
        topic = "test/topic"
        payload = b"Hello, MQTT!"
        retained = False
        
        self.proxy_manager.send_mqtt_message(topic, payload, retained)
        
        self.mock_interface.sendMqttClientProxyMessage.assert_called_once_with(
            topic=topic, data=payload, retained=retained
        )
    
    def test_send_udp_message_text(self):
        """Test sending UDP text message."""
        host = "192.168.1.100"
        port = 1234
        payload = "Hello, UDP!"
        
        self.proxy_manager.send_udp_message(host, port, payload)
        
        self.mock_interface.sendUdpClientProxyMessage.assert_called_once_with(
            host=host, port=port, text=payload
        )
    
    def test_send_udp_message_data(self):
        """Test sending UDP binary message."""
        host = "192.168.1.100"
        port = 1234
        payload = b"Hello, UDP!"
        
        self.proxy_manager.send_udp_message(host, port, payload)
        
        self.mock_interface.sendUdpClientProxyMessage.assert_called_once_with(
            host=host, port=port, data=payload
        )
    
    def test_handle_mqtt_proxy_message(self):
        """Test handling incoming MQTT proxy messages."""
        topic = "test/topic"
        callback = Mock()
        self.proxy_manager.start_mqtt_proxy(topic, callback)
        
        # Create mock proxy message
        proxy_msg = Mock()
        proxy_msg.topic = topic
        proxy_msg.retained = True
        proxy_msg.HasField.return_value = True
        proxy_msg.text = "Hello from device!"
        
        # Simulate message reception
        self.proxy_manager._handle_mqtt_proxy_message(proxy_msg, self.mock_interface)
        
        callback.assert_called_once_with(topic, "Hello from device!", True)
    
    def test_handle_udp_proxy_message(self):
        """Test handling incoming UDP proxy messages."""
        host = "192.168.1.100"
        port = 1234
        callback = Mock()
        self.proxy_manager.start_udp_proxy(host, port, callback)
        
        # Create mock proxy message
        proxy_msg = Mock()
        proxy_msg.topic = f"udp:{host}:{port}"
        proxy_msg.HasField.return_value = True
        proxy_msg.data = b"Hello from device!"
        
        # Simulate message reception
        self.proxy_manager._handle_udp_proxy_message(proxy_msg, self.mock_interface)
        
        callback.assert_called_once_with(host, port, b"Hello from device!")
    
    def test_handle_udp_proxy_message_invalid_topic(self):
        """Test handling UDP proxy message with invalid topic format."""
        callback = Mock()
        
        # Create mock proxy message with invalid topic
        proxy_msg = Mock()
        proxy_msg.topic = "invalid:topic:format"
        
        # Should not crash, just log warning
        self.proxy_manager._handle_udp_proxy_message(proxy_msg, self.mock_interface)
        
        callback.assert_not_called()
    
    def test_get_mqtt_topics(self):
        """Test getting list of MQTT topics."""
        topics = ["topic1", "topic2", "topic3"]
        for topic in topics:
            self.proxy_manager.start_mqtt_proxy(topic, Mock())
        
        result = self.proxy_manager.get_mqtt_topics()
        self.assertEqual(set(result), set(topics))
    
    def test_get_udp_destinations(self):
        """Test getting list of UDP destinations."""
        destinations = [("192.168.1.100", 1234), ("10.0.0.1", 5678)]
        for host, port in destinations:
            self.proxy_manager.start_udp_proxy(host, port, Mock())
        
        result = self.proxy_manager.get_udp_destinations()
        result_tuples = [(dest[0], int(dest[1])) for dest in result]
        self.assertEqual(set(result_tuples), set(destinations))
    
    def test_stop_all_proxies(self):
        """Test stopping all proxies."""
        # Start some proxies
        self.proxy_manager.start_mqtt_proxy("topic1", Mock())
        self.proxy_manager.start_mqtt_proxy("topic2", Mock())
        self.proxy_manager.start_udp_proxy("192.168.1.100", 1234, Mock())
        
        # Verify they're started
        self.assertEqual(len(self.proxy_manager.mqtt_handlers), 2)
        self.assertEqual(len(self.proxy_manager.udp_handlers), 1)
        
        # Stop all
        self.proxy_manager.stop_all_proxies()
        
        # Verify they're stopped
        self.assertEqual(len(self.proxy_manager.mqtt_handlers), 0)
        self.assertEqual(len(self.proxy_manager.udp_handlers), 0)


class TestMeshInterfaceProxyMethods(unittest.TestCase):
    """Test cases for proxy methods in MeshInterface."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_interface = Mock(spec=MeshInterface)
        self.mock_interface._sendToRadio = Mock()

    def test_send_mqtt_client_proxy_message_text(self):
        """Test sending MQTT proxy message with text."""
        from meshtastic.mesh_interface import MeshInterface

        # Call the actual method
        MeshInterface.sendMqttClientProxyMessage(
            self.mock_interface,
            topic="test/topic",
            text="Hello MQTT",
            retained=True
        )

        # Verify _sendToRadio was called
        self.mock_interface._sendToRadio.assert_called_once()

        # Get the ToRadio message that was sent
        call_args = self.mock_interface._sendToRadio.call_args[0][0]
        self.assertTrue(call_args.HasField("mqttClientProxyMessage"))

        proxy_msg = call_args.mqttClientProxyMessage
        self.assertEqual(proxy_msg.topic, "test/topic")
        self.assertEqual(proxy_msg.text, "Hello MQTT")
        self.assertEqual(proxy_msg.retained, True)

    def test_send_mqtt_client_proxy_message_data(self):
        """Test sending MQTT proxy message with binary data."""
        from meshtastic.mesh_interface import MeshInterface

        # Call the actual method
        MeshInterface.sendMqttClientProxyMessage(
            self.mock_interface,
            topic="test/topic",
            data=b"Hello MQTT",
            retained=False
        )

        # Verify _sendToRadio was called
        self.mock_interface._sendToRadio.assert_called_once()

        # Get the ToRadio message that was sent
        call_args = self.mock_interface._sendToRadio.call_args[0][0]
        self.assertTrue(call_args.HasField("mqttClientProxyMessage"))

        proxy_msg = call_args.mqttClientProxyMessage
        self.assertEqual(proxy_msg.topic, "test/topic")
        self.assertEqual(proxy_msg.data, b"Hello MQTT")
        self.assertEqual(proxy_msg.retained, False)

    def test_send_udp_client_proxy_message_text(self):
        """Test sending UDP proxy message with text."""
        from meshtastic.mesh_interface import MeshInterface

        # Call the actual method
        MeshInterface.sendUdpClientProxyMessage(
            self.mock_interface,
            host="192.168.1.100",
            port=1234,
            text="Hello UDP"
        )

        # Verify _sendToRadio was called
        self.mock_interface._sendToRadio.assert_called_once()

        # Get the ToRadio message that was sent
        call_args = self.mock_interface._sendToRadio.call_args[0][0]
        self.assertTrue(call_args.HasField("mqttClientProxyMessage"))

        proxy_msg = call_args.mqttClientProxyMessage
        self.assertEqual(proxy_msg.topic, "udp:192.168.1.100:1234")
        self.assertEqual(proxy_msg.text, "Hello UDP")
        self.assertEqual(proxy_msg.retained, False)

    def test_send_udp_client_proxy_message_data(self):
        """Test sending UDP proxy message with binary data."""
        from meshtastic.mesh_interface import MeshInterface

        # Call the actual method
        MeshInterface.sendUdpClientProxyMessage(
            self.mock_interface,
            host="10.0.0.1",
            port=5678,
            data=b"Hello UDP"
        )

        # Verify _sendToRadio was called
        self.mock_interface._sendToRadio.assert_called_once()

        # Get the ToRadio message that was sent
        call_args = self.mock_interface._sendToRadio.call_args[0][0]
        self.assertTrue(call_args.HasField("mqttClientProxyMessage"))

        proxy_msg = call_args.mqttClientProxyMessage
        self.assertEqual(proxy_msg.topic, "udp:10.0.0.1:5678")
        self.assertEqual(proxy_msg.data, b"Hello UDP")
        self.assertEqual(proxy_msg.retained, False)

    def test_proxy_message_validation_errors(self):
        """Test validation errors in proxy methods."""
        from meshtastic.mesh_interface import MeshInterface

        # Test both data and text specified
        with self.assertRaises(ValueError):
            MeshInterface.sendMqttClientProxyMessage(
                self.mock_interface,
                topic="test",
                data=b"data",
                text="text"
            )

        # Test neither data nor text specified
        with self.assertRaises(ValueError):
            MeshInterface.sendMqttClientProxyMessage(
                self.mock_interface,
                topic="test"
            )

        # Same tests for UDP
        with self.assertRaises(ValueError):
            MeshInterface.sendUdpClientProxyMessage(
                self.mock_interface,
                host="localhost",
                port=1234,
                data=b"data",
                text="text"
            )

        with self.assertRaises(ValueError):
            MeshInterface.sendUdpClientProxyMessage(
                self.mock_interface,
                host="localhost",
                port=1234
            )


if __name__ == "__main__":
    unittest.main()
