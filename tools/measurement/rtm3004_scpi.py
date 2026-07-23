#!/usr/bin/env python3
"""Minimal raw SCPI/TCP client for the RTM3004 measurement tools."""

from __future__ import annotations

import os
import socket
from typing import Any


DEFAULT_HOST = os.environ.get("RTM3004_IP", "192.168.50.2")
DEFAULT_PORT = int(os.environ.get("RTM3004_PORT", "5025"))


class ScpiSocket:
    def __init__(self, host: str, port: int, timeout: float) -> None:
        self._socket = socket.create_connection((host, port), timeout=timeout)
        self._socket.settimeout(timeout)
        self._reader = self._socket.makefile("rb")

    def write(self, command: str) -> None:
        if "\r" in command or "\n" in command:
            raise ValueError("SCPI command must be a single line")
        self._socket.sendall((command + "\n").encode("ascii"))

    def query(self, command: str) -> str:
        if "?" not in command:
            raise ValueError(f"SCPI query must contain '?': {command}")

        self.write(command)
        response = self._reader.readline()
        if not response:
            raise ConnectionError(f"No response for SCPI query: {command}")
        return response.decode("ascii", errors="replace").strip()

    def query_binary_block(self, command: str) -> bytes:
        if "?" not in command:
            raise ValueError(f"SCPI query must contain '?': {command}")

        self.write(command)
        marker = self._read_exact(1)
        if marker != b"#":
            response = marker + self._reader.readline()
            decoded = response.decode("ascii", errors="replace").strip()
            raise ValueError(f"Expected an IEEE binary block, received: {decoded!r}")

        digit_count_raw = self._read_exact(1)
        if not digit_count_raw.isdigit():
            raise ValueError(f"Invalid IEEE block length digit: {digit_count_raw!r}")

        digit_count = int(digit_count_raw)
        if digit_count == 0:
            raise ValueError("Indefinite-length IEEE blocks are not supported")

        payload_length_raw = self._read_exact(digit_count)
        try:
            payload_length = int(payload_length_raw)
        except ValueError as exc:
            raise ValueError(
                f"Invalid IEEE block payload length: {payload_length_raw!r}"
            ) from exc

        payload = self._read_exact(payload_length)
        terminator = self._read_exact(1)
        if terminator == b"\r":
            terminator += self._read_exact(1)
        if terminator not in (b"\n", b"\r\n"):
            raise ValueError(f"Unexpected IEEE block terminator: {terminator!r}")
        return payload

    def _read_exact(self, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = self._reader.read(remaining)
            if not chunk:
                raise ConnectionError(
                    f"SCPI connection closed with {remaining} bytes still expected"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        self._reader.close()
        self._socket.close()

    def __enter__(self) -> "ScpiSocket":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
