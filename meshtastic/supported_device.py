"""Supported Meshtastic Devices - This is a class and collection of Meshtastic devices.
It is used for auto detection as to which device might be connected.
"""

from dataclasses import dataclass, field

# Goal is to detect which device and port to use from the supported devices
# without installing any libraries that are not currently in the python meshtastic library


@dataclass(eq=False)
class SupportedDevice:
    """Devices supported on Meshtastic."""

    name: str
    version: str | None = None
    for_firmware: str | None = None
    device_class: str = "esp32"  # could be "nrf52"
    baseport_on_linux: str | None = None  # ex: ttyUSB or ttyACM
    baseport_on_mac: str | None = None
    baseport_on_windows: str = "COM"
    # when you run "lsusb -d xxxx:" in linux
    usb_vendor_id_in_hex: str | None = None  # store in lower case
    usb_product_id_in_hex: str | None = None  # store in lower case
    usb_id_aliases: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    """Alternate VID/PID pairs for known USB enumeration modes."""

    def __post_init__(self) -> None:
        """Normalize USB ID fields to lowercase and canonical tuple form."""
        self.usb_vendor_id_in_hex = (
            self.usb_vendor_id_in_hex.lower() if self.usb_vendor_id_in_hex else None
        )
        self.usb_product_id_in_hex = (
            self.usb_product_id_in_hex.lower() if self.usb_product_id_in_hex else None
        )
        normalized_aliases: list[tuple[str, str]] = []
        for vendor_id, product_id in self.usb_id_aliases:
            normalized_vendor_id = vendor_id.lower().strip() if vendor_id else ""
            normalized_product_id = product_id.lower().strip() if product_id else ""
            if normalized_vendor_id and normalized_product_id:
                normalized_aliases.append((normalized_vendor_id, normalized_product_id))
        self.usb_id_aliases = tuple(normalized_aliases)

    @property
    def usb_ids(self) -> tuple[tuple[str, str], ...]:
        """Return primary and alternate normalized VID/PID pairs for detection."""
        usb_ids: list[tuple[str, str]] = []
        if self.usb_vendor_id_in_hex and self.usb_product_id_in_hex:
            usb_ids.append((self.usb_vendor_id_in_hex, self.usb_product_id_in_hex))
        usb_ids.extend(self.usb_id_aliases)
        return tuple(dict.fromkeys(usb_ids))


# supported devices
tbeam_v0_7 = SupportedDevice(
    name="T-Beam",
    version="0.7",
    for_firmware="tbeam0.7",
    baseport_on_linux="ttyACM",
    baseport_on_mac="cu.usbmodem",
    usb_vendor_id_in_hex="1a86",
    usb_product_id_in_hex="55d4",
)
tbeam_v1_1 = SupportedDevice(
    name="T-Beam",
    version="1.1",
    for_firmware="tbeam",
    baseport_on_linux="ttyACM",
    baseport_on_mac="cu.usbmodem",
    usb_vendor_id_in_hex="1a86",
    usb_product_id_in_hex="55d4",
)
tbeam_M8N = SupportedDevice(
    name="T-Beam",
    version="M8N",
    for_firmware="tbeam",
    baseport_on_linux="ttyACM",
    baseport_on_mac="cu.usbmodem",
    usb_vendor_id_in_hex="1a86",
    usb_product_id_in_hex="55d4",
)
tbeam_M8N_SX1262 = SupportedDevice(
    name="T-Beam",
    version="M8N_SX1262",
    for_firmware="tbeam",
    baseport_on_linux="ttyACM",
    baseport_on_mac="cu.usbmodem",
    usb_vendor_id_in_hex="1a86",
    usb_product_id_in_hex="55d4",
)
tlora_v1 = SupportedDevice(
    name="T-Lora",
    version="1",
    for_firmware="tlora-v1",
    baseport_on_linux="ttyUSB",
    baseport_on_mac="cu.usbserial",
    usb_vendor_id_in_hex="1a86",
    usb_product_id_in_hex="55d4",
)
tlora_v1_3 = SupportedDevice(
    name="T-Lora",
    version="1.3",
    for_firmware="tlora-v1-3",
    baseport_on_linux="ttyUSB",
    baseport_on_mac="cu.usbserial",
    usb_vendor_id_in_hex="10c4",
    usb_product_id_in_hex="ea60",
)
tlora_v2 = SupportedDevice(
    name="T-Lora",
    version="2",
    for_firmware="tlora-v2",
    baseport_on_linux="ttyACM",
    baseport_on_mac="cu.usbmodem",
    usb_vendor_id_in_hex="1a86",
    usb_product_id_in_hex="55d4",
)
tlora_v2_1_1_6 = SupportedDevice(
    name="T-Lora",
    version="2.1-1.6",
    for_firmware="tlora-v2-1-1.6",
    baseport_on_linux="ttyACM",
    baseport_on_mac="cu.usbmodem",
    usb_vendor_id_in_hex="1a86",
    usb_product_id_in_hex="55d4",
)
heltec_v1 = SupportedDevice(
    name="Heltec",
    version="1",
    for_firmware="heltec-v1",
    baseport_on_linux="ttyUSB",
    baseport_on_mac="cu.usbserial-",
    usb_vendor_id_in_hex="10c4",
    usb_product_id_in_hex="ea60",
)
heltec_v2_0 = SupportedDevice(
    name="Heltec",
    version="2.0",
    for_firmware="heltec-v2.0",
    baseport_on_linux="ttyUSB",
    baseport_on_mac="cu.usbserial-",
    usb_vendor_id_in_hex="10c4",
    usb_product_id_in_hex="ea60",
)
heltec_v2_1 = SupportedDevice(
    name="Heltec",
    version="2.1",
    for_firmware="heltec-v2.1",
    baseport_on_linux="ttyUSB",
    baseport_on_mac="cu.usbserial-",
    usb_vendor_id_in_hex="10c4",
    usb_product_id_in_hex="ea60",
)
rak11200 = SupportedDevice(
    name="RAK 11200",
    version=None,
    for_firmware="rak11200",
    baseport_on_linux="ttyUSB",
    baseport_on_mac="cu.usbserial-",
    usb_vendor_id_in_hex="1a86",
    usb_product_id_in_hex="7523",
)
meshtastic_diy_v1 = SupportedDevice(
    name="Meshtastic DIY",
    version="1",
    for_firmware="meshtastic-diy-v1",
    baseport_on_linux="ttyUSB",
    baseport_on_mac="cu.usbserial-",
    usb_vendor_id_in_hex="10c4",
    usb_product_id_in_hex="ea60",
)
# Note: The T-Echo reports product id in boot mode
techo_1 = SupportedDevice(
    name="T-Echo",
    version="1",
    for_firmware="t-echo-1",
    device_class="nrf52",
    baseport_on_linux="ttyACM",
    baseport_on_mac="cu.usbmodem",
    usb_vendor_id_in_hex="239a",
    usb_product_id_in_hex="0029",
)
rak4631_5005 = SupportedDevice(
    name="RAK 4631 5005",
    version=None,
    for_firmware="rak4631_5005",
    device_class="nrf52",
    baseport_on_linux="ttyACM",
    baseport_on_mac="cu.usbmodem",
    usb_vendor_id_in_hex="239a",
    usb_product_id_in_hex="0029",
)
rak4631_5005_epaper = SupportedDevice(
    name="RAK 4631 5005 14000 epaper",
    version=None,
    for_firmware="rak4631_5005_epaper",
    device_class="nrf52",
    baseport_on_linux="ttyACM",
    baseport_on_mac="cu.usbmodem",
    usb_vendor_id_in_hex="239a",
    usb_product_id_in_hex="0029",
)
# Note: The 19003 reports same product id as 5005 in boot mode
rak4631_19003 = SupportedDevice(
    name="RAK 4631 19003",
    version=None,
    for_firmware="rak4631_19003",
    device_class="nrf52",
    baseport_on_linux="ttyACM",
    baseport_on_mac="cu.usbmodem",
    usb_vendor_id_in_hex="239a",
    usb_product_id_in_hex="8029",
)
nano_g1 = SupportedDevice(
    name="Nano G1",
    version=None,
    for_firmware="nano-g1",
    baseport_on_linux="ttyACM",
    baseport_on_mac="cu.usbmodem",
    usb_vendor_id_in_hex="1a86",
    usb_product_id_in_hex="55d4",
)

seeed_xiao_s3 = SupportedDevice(
    name="Seeed Xiao ESP32-S3",
    version=None,
    for_firmware="seeed-xiao-esp32s3",
    baseport_on_linux="ttyACM",
    baseport_on_mac="cu.usbmodem",
    usb_vendor_id_in_hex="2886",
    usb_product_id_in_hex="0059",
    # Alternate enumeration mode: native Espressif USB Serial/JTAG.
    usb_id_aliases=(("303a", "1001"),),
)

tdeck = SupportedDevice(
    name="T-Deck",
    version=None,
    for_firmware="t-deck",  # Confirmed firmware identifier
    device_class="esp32",
    baseport_on_linux="ttyACM",
    baseport_on_mac="cu.usbmodem",
    baseport_on_windows="COM",
    usb_vendor_id_in_hex="303a",  # Espressif Systems (VERIFIED)
    usb_product_id_in_hex="1001",  # VERIFIED from actual device
    # Alternate enumeration mode observed with CH9102 USB-UART bridge variants.
    usb_id_aliases=(("1a86", "55d4"),),
)


supported_devices = [
    tbeam_v0_7,
    tbeam_v1_1,
    tbeam_M8N,
    tbeam_M8N_SX1262,
    tlora_v1,
    tlora_v1_3,
    tlora_v2,
    tlora_v2_1_1_6,
    heltec_v1,
    heltec_v2_0,
    heltec_v2_1,
    meshtastic_diy_v1,
    techo_1,
    rak4631_5005,
    rak4631_5005_epaper,
    rak4631_19003,
    rak11200,
    nano_g1,
    seeed_xiao_s3,
    tdeck,  # T-Deck support added
]
