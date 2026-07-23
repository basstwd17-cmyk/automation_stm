"""USB CDC command client for the STM32 delay generator."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Event

try:
    import serial
    from serial.tools import list_ports
except ModuleNotFoundError:  # The UI can still open and show a useful error.
    serial = None
    list_ports = None


STM32_VID = 0x0483
STM32_CDC_PID = 0x5740


class DeviceError(RuntimeError):
    pass


@dataclass(frozen=True)
class DelaySetting:
    requested_ps: int
    signed_ticks: int
    actual_ps: int


@dataclass(frozen=True)
class DeviceStatus:
    state: str
    count: int
    target: int
    requested_ps: int
    signed_ticks: int
    actual_ps: int


def _require_pyserial() -> None:
    if serial is None or list_ports is None:
        raise DeviceError(
            "pyserial is required. Run: py -3 -m pip install pyserial"
        )


def _response_fields(response: str, response_type: str) -> dict[str, str]:
    prefix = f"OK {response_type}"
    if not response.startswith(prefix):
        if response.startswith("ERR "):
            raise DeviceError(response)
        raise DeviceError(f"Unexpected device response: {response!r}")

    fields: dict[str, str] = {}
    for token in response[len(prefix):].strip().split():
        if "=" in token:
            key, value = token.split("=", 1)
            fields[key] = value
    return fields


def available_ports() -> list[str]:
    _require_pyserial()
    return [port.device for port in list_ports.comports()]


def find_stm32_port() -> str:
    _require_pyserial()
    matches = [
        port.device
        for port in list_ports.comports()
        if port.vid == STM32_VID and port.pid == STM32_CDC_PID
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise DeviceError("STM32 CDC port was not found")
    raise DeviceError(f"Multiple STM32 CDC ports found: {', '.join(matches)}")


class DelayGenerator:
    def __init__(self, port: str, timeout_s: float = 2.0) -> None:
        _require_pyserial()
        selected_port = find_stm32_port() if port.upper() == "AUTO" else port
        self.port = selected_port
        self.timeout_s = timeout_s
        serial_port = serial.Serial(
            port=selected_port,
            baudrate=115200,
            timeout=0.1,
            write_timeout=timeout_s,
        )
        try:
            time.sleep(0.2)
            serial_port.reset_input_buffer()
            serial_port.reset_output_buffer()
        except Exception:
            serial_port.close()
            raise
        self._serial = serial_port

    @classmethod
    def connect(
        cls,
        port: str,
        timeout_s: float = 2.0,
        wait_s: float = 10.0,
    ) -> "DelayGenerator":
        deadline = time.monotonic() + wait_s
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            device: DelayGenerator | None = None
            try:
                device = cls(port, timeout_s)
                device.ping()
                return device
            except (DeviceError, OSError) as error:
                if device is not None:
                    device.close()
                last_error = error
                time.sleep(0.25)
        raise DeviceError(f"Could not connect to STM32 CDC: {last_error}")

    def close(self) -> None:
        self._serial.close()

    def command(self, command: str) -> str:
        self._serial.write((command.strip() + "\n").encode("ascii"))
        self._serial.flush()
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            raw = self._serial.readline()
            if not raw:
                continue
            response = raw.decode("ascii", errors="replace").strip()
            if response:
                if response.startswith("ERR "):
                    raise DeviceError(response)
                return response
        raise DeviceError(f"No STM32 response for {command!r}")

    def ping(self) -> None:
        response = self.command("PING")
        if response != "OK PONG":
            raise DeviceError(f"Unexpected PING response: {response!r}")

    def set_delay(self, delay_ps: int) -> DelaySetting:
        fields = _response_fields(self.command(f"SET {delay_ps}"), "SET")
        try:
            return DelaySetting(
                requested_ps=int(fields["REQUEST_PS"]),
                signed_ticks=int(fields["TICKS"]),
                actual_ps=int(fields["ACTUAL_PS"]),
            )
        except (KeyError, ValueError) as error:
            raise DeviceError(f"Invalid SET response fields: {fields}") from error

    def arm(self, pulse_count: int) -> None:
        fields = _response_fields(self.command(f"ARM {pulse_count}"), "ARM")
        if int(fields.get("TARGET", "0")) != pulse_count:
            raise DeviceError(f"ARM target mismatch: {fields}")

    def status(self) -> DeviceStatus:
        fields = _response_fields(self.command("STATUS"), "STATUS")
        try:
            return DeviceStatus(
                state=fields["STATE"],
                count=int(fields["COUNT"]),
                target=int(fields["TARGET"]),
                requested_ps=int(fields["REQUEST_PS"]),
                signed_ticks=int(fields["TICKS"]),
                actual_ps=int(fields["ACTUAL_PS"]),
            )
        except (KeyError, ValueError) as error:
            raise DeviceError(f"Invalid STATUS response fields: {fields}") from error

    def stop(self) -> int:
        fields = _response_fields(self.command("STOP"), "STOP")
        return int(fields.get("COUNT", "0"))

    def wait_done(
        self,
        target: int,
        timeout_s: float,
        abort_event: Event,
    ) -> DeviceStatus:
        deadline = time.monotonic() + timeout_s
        last_status = self.status()
        while time.monotonic() < deadline:
            if abort_event.is_set():
                self.stop()
                raise DeviceError("Measurement aborted")
            last_status = self.status()
            if last_status.state == "DONE":
                if last_status.count != target:
                    raise DeviceError(
                        f"STM32 count mismatch: {last_status.count}/{target}"
                    )
                return last_status
            time.sleep(0.02)
        raise DeviceError(
            f"STM32 count timeout: {last_status.count}/{target}, "
            f"state={last_status.state}"
        )
