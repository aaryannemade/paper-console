"""Native USB printer-class adapter for the development USB output.

The printer's built-in USB port is a USB class 7 bulk endpoint, *not* a
USB-to-serial adapter. This adapter presents the small write/flush/close
interface used by UsbSerialOutput, so both development USB connections send the
same CSN-A2 raster strips from app.drivers.escpos.
"""

import logging
import os

from app.drivers.print_outputs import PrinterUnavailableError

logger = logging.getLogger(__name__)

VENDOR_ENV = "PC1_PRINTER_USB_VENDOR_ID"
PRODUCT_ENV = "PC1_PRINTER_USB_PRODUCT_ID"
LIBUSB_ENV = "PC1_LIBUSB_LIBRARY"


def _backend():
    import usb.backend.libusb1

    library = os.environ.get(LIBUSB_ENV, "").strip()
    if library:
        backend = usb.backend.libusb1.get_backend(find_library=lambda _: library)
    else:
        backend = usb.backend.libusb1.get_backend()
    if backend is None:
        raise PrinterUnavailableError("libusb-1.0 is unavailable (set PC1_LIBUSB_LIBRARY)")
    return backend


def _ids_from_env():
    raw_vendor = os.environ.get(VENDOR_ENV, "").strip()
    raw_product = os.environ.get(PRODUCT_ENV, "").strip()
    if bool(raw_vendor) != bool(raw_product):
        raise ValueError(f"Set both {VENDOR_ENV} and {PRODUCT_ENV} together")
    if not raw_vendor:
        return None
    try:
        vendor, product = (int(value, 16) for value in (raw_vendor, raw_product))
    except ValueError as exc:
        raise ValueError("USB vendor/product IDs must be hexadecimal") from exc
    if not (0 <= vendor <= 0xFFFF and 0 <= product <= 0xFFFF):
        raise ValueError("USB vendor/product IDs must be 16-bit hexadecimal numbers")
    return vendor, product


def printer_interface(device):
    """Find a USB printer interface (class 7) with a bulk OUT endpoint."""
    import usb.util

    for configuration in device:
        for interface in configuration:
            if interface.bInterfaceClass != 7:
                continue
            endpoint = usb.util.find_descriptor(
                interface,
                custom_match=lambda ep: usb.util.endpoint_direction(ep.bEndpointAddress)
                == usb.util.ENDPOINT_OUT
                and usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK,
            )
            if endpoint is not None:
                return configuration, interface, endpoint
    return None


def find_native_printer():
    """Find the printer-class USB device; fail if more than one is connected."""
    import usb.core

    ids = _ids_from_env()
    filters = {"idVendor": ids[0], "idProduct": ids[1]} if ids else {}
    printers = [
        device for device in (usb.core.find(find_all=True, backend=_backend(), **filters) or [])
        if printer_interface(device) is not None
    ]
    if len(printers) > 1:
        raise PrinterUnavailableError(
            f"Multiple USB printers found; select one with {VENDOR_ENV} and {PRODUCT_ENV}"
        )
    return printers[0] if printers else None


class UsbBulkPort:
    """Minimal serial-like adapter over a native USB printer bulk endpoint."""

    def __init__(self, device):
        import usb.core
        import usb.util

        self.device = device
        self.is_open = False
        self.in_waiting = 0
        self.timeout = 1
        self.port = f"usb:{device.idVendor:04x}:{device.idProduct:04x}"
        configuration, interface, endpoint = printer_interface(device)
        self.endpoint = endpoint.bEndpointAddress
        self.interface = interface.bInterfaceNumber
        try:
            if device.get_active_configuration().bConfigurationValue != configuration.bConfigurationValue:
                device.set_configuration(configuration.bConfigurationValue)
            if device.is_kernel_driver_active(self.interface):
                raise PrinterUnavailableError(
                    "USB printer is claimed by a kernel driver; use another interface or release it"
                )
            usb.util.claim_interface(device, self.interface)
        except usb.core.USBError as exc:
            raise PrinterUnavailableError(f"Cannot access {self.port}: {exc}") from exc
        self.is_open = True

    def write(self, data):
        if not self.is_open:
            raise OSError("USB printer is closed")
        # USB bulk flow control is handled by the device. The caller also
        # limits each write to 128 bytes and checks for partial writes.
        return self.device.write(self.endpoint, bytes(data), timeout=5000)

    def flush(self):
        # A completed bulk write was accepted by the device; there is no
        # UART-side tcdrain on the host's USB connection.
        return None

    def read(self, size=1):
        # Do not probe the optional USB IN endpoint during a receipt. A status
        # query may not be implemented by every USB printer firmware.
        return b""

    def reset_output_buffer(self):
        # Resetting the USB endpoint can discard data already sent to the printer.
        pass

    def close(self):
        if self.is_open:
            import usb.util

            self.is_open = False
            usb.util.dispose_resources(self.device)
