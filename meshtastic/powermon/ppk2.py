"""Classes for logging power consumption of meshtastic devices."""

import logging
import threading
import time
from contextlib import suppress
from typing import Final

from ppk2_api import ppk2_api  # type: ignore[import-untyped]

from .constants import MICROAMPS_PER_MILLIAMP, MILLIVOLTS_PER_VOLT, MIN_SUPPLY_VOLTAGE_V
from .power_supply import PowerError, PowerSupply

# PPK2-specific timing constants
INITIAL_POLL_TIMEOUT_S: Final[float] = 0.0001  # Initial poll timeout (100μs).
SUBSEQUENT_POLL_TIMEOUT_S: Final[float] = 0.001  # Subsequent poll timeout (1ms).
THREAD_JOIN_TIMEOUT_S: Final[float] = 5.0  # Join timeout for measurement thread.
STABILIZATION_DELAY_S: Final[float] = 0.2  # Delay to discard initial FIFO readings.


class PPK2PowerSupply(PowerSupply):
    """Interface for talking with the NRF PPK2 high-resolution micro-power supply.

    Power Profiler Kit II is what you should google to find it for purchase.
    """

    def __init__(self, portName: str | None = None) -> None:
        """Initialize a PPK2PowerSupply and prepare it for measurements.

        If portName is None the constructor auto-discovers connected PPK2 devices
        and selects the single available device; it raises PowerError if no devices
        are found or if multiple devices are present. Opens a PPK2_API connection to
        the device, initializes measurement state and synchronization primitives, creates
        (but does not start) the background measurement thread, logs the connection,
        and then calls the superclass initializer.

        Parameters
        ----------
        portName : str | None
            Serial port or device identifier for the PPK2
            device. If None, the constructor attempts to auto-discover a single
            connected PPK2 device; provide a value to select a specific device. (Default value = None)

        Raises
        ------
        PowerError
            If no PPK2 devices are found when portName is None.
        PowerError
            If multiple PPK2 devices are found when portName is None.
        """
        if portName is None:
            devs = ppk2_api.PPK2_API.list_devices()
            if not devs:
                raise PowerError("No PPK2 devices found")  # noqa: TRY003
            if len(devs) > 1:
                raise PowerError(  # noqa: TRY003
                    "Multiple PPK2 devices found, please specify the portName"
                )
            portName = devs[0]

        self.measuring = False
        self.current_max = 0
        self.current_min = 0
        self.current_sum = 0
        self.current_num_samples = 0
        self.current_average: float = 0.0
        self.last_reported_min: float = 0.0
        self.last_reported_max: float = 0.0

        # for tracking avera data read length (to determine if we are sleeping efficiently in measurement_loop)
        self.total_data_len = 0
        self.num_data_reads = 0
        self.max_data_len = 0

        # Normally we just sleep with a timeout on this condition (polling the power measurement data repeatedly)
        # but any time our measurements have been fully consumed (via reset_measurements) we notify() this condition
        # to trigger a new reading ASAP.
        self._want_measurement = threading.Condition()

        # To guard against a brief window while updating measured values
        self._result_lock = threading.Condition()

        self.r = r = ppk2_api.PPK2_API(
            portName
        )  # serial port will be different for you
        r.get_modifiers()

        self.measurement_thread = threading.Thread(
            target=self._measurement_loop, daemon=True, name="ppk2 measurement"
        )
        logging.info("Connected to Power Profiler Kit II (PPK2)")
        super().__init__()  # we call this late so that the serial port is already open

    def _measurement_loop(self) -> None:
        """Endless measurement loop that runs in a background thread.

        Continuously polls the PPK2 device for current samples, updating
        min/max/sum statistics under ``_result_lock`` so first-batch
        initialization after ``reset_measurements()`` cannot race.
        """
        while self.measuring:
            with self._want_measurement:
                self._want_measurement.wait(
                    INITIAL_POLL_TIMEOUT_S
                    if self.num_data_reads == 0
                    else SUBSEQUENT_POLL_TIMEOUT_S
                )
                if not self.measuring:
                    break
                # normally we poll using this timeout, but sometimes
                # reset_measurement() will notify us to read immediately

                # always reads 4096 bytes, even if there is no new samples - or possibly the python single thread (because of global interpreter lock)
                # is always behind and thefore we are inherently dropping samples semi randomly!!!
                read_data = self.r.get_data()
                if read_data != b"":
                    samples, _ = self.r.get_samples(read_data)

                    # update invariants
                    if len(samples) > 0:
                        # The following operations could be expensive, so do outside of the lock
                        # FIXME - change all these lists into numpy arrays to use lots less CPU
                        batch_max = max(samples)
                        batch_min = min(samples)
                        latest_sum = sum(samples)
                        with self._result_lock:
                            if self.current_num_samples == 0:
                                # First set of new reads, reset min/max
                                self.current_max = batch_max
                                self.current_min = batch_min
                            else:
                                self.current_max = max(self.current_max, batch_max)
                                self.current_min = min(self.current_min, batch_min)
                            self.current_sum += latest_sum
                            self.current_num_samples += len(samples)
                        # logging.debug(f"PPK2 data_len={len(read_data)}, sample_len={len(samples)}")

                self.num_data_reads += 1
                self.total_data_len += len(read_data)
                self.max_data_len = max(self.max_data_len, len(read_data))

    def getMinCurrentMA(self) -> float:
        """Return the minimum current reading in milliamperes.

        Returns
        -------
        float
            Minimum current in mA. If there are no new samples since the last
            reset, returns the last reported minimum to avoid transient empty-window values.
        """
        with self._result_lock:
            if self.current_num_samples != 0:
                self.last_reported_min = self.current_min
            return self.last_reported_min / MICROAMPS_PER_MILLIAMP

    def getMaxCurrentMA(self) -> float:
        """Return the maximum current reading in milliamperes.

        Returns
        -------
        float
            Maximum current in mA. If there are no new samples since the last
            reset, returns the last reported maximum to avoid transient empty-window values.
        """
        with self._result_lock:
            if self.current_num_samples != 0:
                self.last_reported_max = self.current_max
            return self.last_reported_max / MICROAMPS_PER_MILLIAMP

    def getAverageCurrentMA(self) -> float:
        """Return the average current reading in milliamperes.

        Returns
        -------
        float
            Average current in mA. If there are no new samples since the last
            reset, returns the last calculated average.
        """
        with self._result_lock:
            if self.current_num_samples != 0:
                # If we have new samples, calculate a new average
                self.current_average = self.current_sum / self.current_num_samples

            # Even if we don't have new samples, return the last calculated average
            # measurements are in microamperes, divide by 1000
            return self.current_average / MICROAMPS_PER_MILLIAMP

    def resetMeasurements(self) -> None:
        """Reset current-window accumulators while preserving last reported extrema."""
        with self._result_lock:
            if self.current_num_samples != 0:
                self.last_reported_min = self.current_min
                self.last_reported_max = self.current_max
            self.current_sum = 0
            self.current_num_samples = 0

        # if self.num_data_reads:
        #    logging.debug(f"max data len = {self.max_data_len},avg {self.total_data_len/self.num_data_reads}, num reads={self.num_data_reads}")
        # Summary stats for performance monitoring
        self.num_data_reads = 0
        self.total_data_len = 0
        self.max_data_len = 0

        with self._want_measurement:
            self._want_measurement.notify()  # notify the measurement loop to read immediately

    def close(self) -> None:
        """Close the power meter and release resources."""
        self.measuring = False
        with self._want_measurement:
            self._want_measurement.notify_all()
        with suppress(Exception):
            self.r.stop_measuring()  # send command to ppk2
        if self.measurement_thread.is_alive():
            self.measurement_thread.join(timeout=THREAD_JOIN_TIMEOUT_S)
            if self.measurement_thread.is_alive():
                logging.warning(
                    "PPK2 measurement thread did not stop within timeout; forcing transport cleanup."
                )
        close_method = getattr(self.r, "close", None)
        if callable(close_method):
            with suppress(Exception):
                close_method()  # pylint: disable=not-callable

        serial_handle = getattr(self.r, "ser", None)
        if serial_handle is not None:
            with suppress(Exception):
                serial_handle.close()
        super().close()

    def setIsSupply(self, is_supply: bool) -> None:
        """Set the PPK2 mode to either power supply or amp meter.

        If in supply mode we will provide power ourself, otherwise we are just an amp meter.

        Parameters
        ----------
        is_supply : bool
            True to enable power supply mode, False for amp meter mode only.

        Raises
        ------
        PowerError
            If the supply voltage has not been set to at least MIN_SUPPLY_VOLTAGE_V before calling this method.
        """

        # setIsSupply validates the preconfigured voltage (self.v) before
        # calling set_source_voltage(); callers should set desired voltage
        # first via set_source_voltage() or direct self.v assignment.
        if self.v < MIN_SUPPLY_VOLTAGE_V:
            raise PowerError(  # noqa: TRY003
                f"Supply voltage must be set to at least {MIN_SUPPLY_VOLTAGE_V}V before calling setIsSupply "
                f"(current v={self.v!r})"
            )

        self.r.set_source_voltage(
            int(self.v * MILLIVOLTS_PER_VOLT)
        )  # set source voltage in mV BEFORE setting source mode
        # Note: source voltage must be set even if we are using the amp meter mode

        # Avoid re-issuing start while actively measuring: some devices flush/restart
        # buffered data if start_measuring() is sent again mid-session.
        is_measuring = self.measurement_thread.is_alive()
        if not is_measuring:
            # must be after setting source voltage and before setting mode
            self.r.start_measuring()  # send command to ppk2

        if (
            not is_supply
        ):  # min power outpuf of PPK2.  If less than this assume we want just meter mode.
            self.r.use_ampere_meter()
        else:
            self.r.use_source_meter()  # set source meter mode

        if not is_measuring:
            self.measuring = True

            # Thread objects are single-use; create a fresh one if the previous
            # thread has already been started (and possibly joined via close()).
            if self.measurement_thread.ident is not None:
                self.measurement_thread = threading.Thread(
                    target=self._measurement_loop, daemon=True, name="ppk2 measurement"
                )
            # We can't start reading from the thread until vdd is set, so start running the thread now
            self.measurement_thread.start()
        else:
            # Preserve measuring intent when switching mode on a running thread.
            self.measuring = True

        # Mode switches can produce transient FIFO samples. Clear windows, allow
        # stabilization, then reset again so post-switch stats start clean.
        self.resetMeasurements()
        time.sleep(STABILIZATION_DELAY_S)
        self.resetMeasurements()

    def powerOn(self) -> None:
        """Power on the DUT (Device Under Test)."""
        self.r.toggle_DUT_power("ON")

    def powerOff(self) -> None:
        """Power off the DUT (Device Under Test)."""
        self.r.toggle_DUT_power("OFF")
