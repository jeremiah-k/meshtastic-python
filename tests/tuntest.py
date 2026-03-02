# delete me eventually
# Note python-pytuntap was too buggy
# using pip3 install pytap2
# make sure to "sudo setcap cap_net_admin+eip /usr/bin/python3.10" so python can access tun device without being root
# sudo ip tuntap del mode tun tun0

# FIXME: set MTU correctly
# select local ip address based on nodeid
# print known node ids as IP addresses

import logging
import threading

from pytap2 import TapDevice

# A list of chatty UDP services we should never accidentally
# forward to our slow network.
UDP_BLACKLIST: set[int] = {
    1900,  # SSDP
    5353,  # multicast DNS
}

# A list of TCP services to block.
TCP_BLACKLIST: set[int] = set()

# A list of protocols we ignore.
PROTOCOL_BLACKLIST: set[int] = {
    0x02,  # IGMP
    0x80,  # Service-Specific Connection-Oriented Protocol in a Multilink and Connectionless Environment
}


def hexstr(barray: bytes | bytearray) -> str:
    """Print a string of hex digits."""
    return ":".join("{:02x}".format(x) for x in barray)


def ipstr(barray: bytes | bytearray) -> str:
    """Print a string of ip digits."""
    return ".".join("{}".format(x) for x in barray)


def readnet_u16(p: bytes | bytearray | memoryview, offset: int) -> int:
    """Read big endian u16 (network byte order)."""
    return p[offset] * 256 + p[offset + 1]


def _internet_checksum(payload: bytes) -> int:
    """Compute RFC 1071 Internet checksum for the provided payload."""
    if len(payload) % 2:
        payload += b"\x00"
    checksum = 0
    for i in range(0, len(payload), 2):
        checksum += (payload[i] << 8) + payload[i + 1]
        checksum = (checksum & 0xFFFF) + (checksum >> 16)
    return (~checksum) & 0xFFFF


def _readtest(tap: TapDevice) -> None:
    """Read packets from a TapDevice and log/filter protocol details."""
    while True:
        p = bytes(tap.read())

        protocol = p[8 + 1]
        srcaddr = p[12:16]
        destaddr = p[16:20]
        subheader = 20
        ignore = False  # Assume we will be forwarding the packet
        if protocol in PROTOCOL_BLACKLIST:
            ignore = True
            logging.debug("Ignoring blacklisted protocol 0x%02x", protocol)
        elif protocol == 0x01:  # ICMP
            icmp_offset = subheader
            icmp_type = p[icmp_offset]
            if icmp_type == 0x08:  # echo request -> echo reply
                logging.warning("Generating fake ping reply")
                icmp_reply = bytearray(p[icmp_offset:])
                icmp_reply[0] = 0x00  # Echo reply type
                icmp_reply[2:4] = b"\x00\x00"
                checksum = _internet_checksum(bytes(icmp_reply))
                icmp_reply[2] = (checksum >> 8) & 0xFF
                icmp_reply[3] = checksum & 0xFF
                pingback = p[:12] + p[16:20] + p[12:16] + bytes(icmp_reply)
                tap.write(pingback)
            else:
                logging.debug("Ignoring ICMP type %d (not echo request)", icmp_type)
        elif protocol == 0x11:  # UDP
            srcport = readnet_u16(p, subheader)
            destport = readnet_u16(p, subheader + 2)
            logging.debug("udp srcport=%d, destport=%d", srcport, destport)
            if destport in UDP_BLACKLIST:
                ignore = True
                logging.debug("ignoring blacklisted UDP port %d", destport)
        elif protocol == 0x06:  # TCP
            srcport = readnet_u16(p, subheader)
            destport = readnet_u16(p, subheader + 2)
            logging.debug("tcp srcport=%d, destport=%d", srcport, destport)
            if destport in TCP_BLACKLIST:
                ignore = True
                logging.debug("ignoring blacklisted TCP port %d", destport)
        else:
            logging.warning(
                "unexpected protocol 0x%02x, src=%s, dest=%s",
                protocol,
                ipstr(srcaddr),
                ipstr(destaddr),
            )

        if not ignore:
            logging.debug(
                "Forwarding packet bytelen=%d src=%s, dest=%s",
                len(p),
                ipstr(srcaddr),
                ipstr(destaddr),
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    tun = TapDevice(mtu=200)
    try:
        # tun.create()
        tun.up()
        tun.ifconfig(address="10.115.1.2", netmask="255.255.0.0")

        reader_thread = threading.Thread(target=_readtest, args=(tun,), daemon=True)
        reader_thread.start()
        input("press return key to quit!")
    finally:
        tun.close()
