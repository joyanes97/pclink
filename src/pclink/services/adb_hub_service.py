# src/pclink/services/adb_hub_service.py
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025 AZHAR ZOUHIR / BYTEDz

import asyncio
import logging
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from .input_service import input_service

log = logging.getLogger(__name__)


class AdbHubService:
    def __init__(self):
        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._active_target: Optional[str] = None
        self._active_device_node: Optional[str] = None
        self._sensitivity: float = 1.0
        self._event_count: int = 0
        self._last_event_text: str = ""

    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def get_status(self) -> Dict[str, Any]:
        return {
            "is_running": self.is_running(),
            "target": self._active_target,
            "device_node": self._active_device_node,
            "sensitivity": self._sensitivity,
            "event_count": self._event_count,
            "last_event": self._last_event_text,
        }

    async def query_devices(self, host: str, port: int = 5555) -> List[Dict[str, Any]]:
        target = f"{host}:{port}"
        await self._ensure_connected(target)

        cmd = ["adb", "-s", target, "shell", "getevent", "-il"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=6.0)
            text = stdout.decode("utf-8", errors="ignore")
            return self._parse_devices(text)
        except Exception as e:
            log.error(f"Failed to query ADB devices from {target}: {e}")
            return []

    def _parse_devices(self, output: str) -> List[Dict[str, Any]]:
        devices: List[Dict[str, Any]] = []
        blocks = output.split("add device ")

        for b in blocks:
            if not b.strip():
                continue
            lines = b.strip().splitlines()
            header = lines[0]
            node_match = re.search(r"(/dev/input/event\d+)", header)
            node = node_match.group(1) if node_match else ""

            name = "Unknown Device"
            is_mouse = False
            is_keyboard = False

            block_text = "\n".join(lines)
            name_match = re.search(r'name:\s*"([^"]+)"', block_text)
            if name_match:
                name = name_match.group(1)

            name_lower = name.lower()
            if "mouse" in name_lower or "cursor" in name_lower or "REL_X" in block_text:
                is_mouse = True
            elif "keyboard" in name_lower or "KEY_" in block_text:
                is_keyboard = True

            if node:
                devices.append(
                    {
                        "node": node,
                        "name": name,
                        "is_mouse": is_mouse,
                        "is_keyboard": is_keyboard,
                    }
                )

        return devices

    async def _ensure_connected(self, target: str) -> bool:
        adb_path = shutil.which("adb")
        if not adb_path:
            raise RuntimeError("ADB executable not found on host machine PATH.")

        proc = await asyncio.create_subprocess_exec(
            "adb",
            "connect",
            target,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        out_str = stdout.decode().strip()
        log.info(f"ADB connect {target}: {out_str}")
        return "connected" in out_str.lower() or "already" in out_str.lower()

    async def start(
        self,
        host: str,
        port: int = 5555,
        device_node: str = "",
        sensitivity: float = 1.0,
    ) -> bool:
        await self.stop()

        target = f"{host}:{port}"
        await self._ensure_connected(target)

        self._active_target = target
        self._active_device_node = device_node
        self._sensitivity = max(0.1, min(sensitivity, 5.0))
        self._event_count = 0
        self._last_event_text = ""

        cmd = ["adb", "-s", target, "shell", "getevent"]
        if device_node:
            cmd.append(device_node)

        log.info(f"Spawning ADB input listener: {' '.join(cmd)}")
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self._reader_task = asyncio.create_task(self._stream_loop())
        return True

    async def stop(self):
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            self._reader_task = None

        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=1.0)
            except Exception:
                if self._process:
                    self._process.kill()
            self._process = None

        self._active_target = None
        self._active_device_node = None
        log.info("ADB input listener stopped.")

    async def _stream_loop(self):
        if not self._process or not self._process.stdout:
            return

        accumulated_dx = 0.0
        accumulated_dy = 0.0

        try:
            while True:
                line_bytes = await self._process.stdout.readline()
                if not line_bytes:
                    break

                line = line_bytes.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                parts = line.split()
                if not parts:
                    continue

                if parts[0].startswith("/dev/input"):
                    parts = parts[1:]

                if len(parts) < 3:
                    continue

                try:
                    type_val = int(parts[0], 16)
                    code_val = int(parts[1], 16)
                    raw_val = int(parts[2], 16)
                    if raw_val >= 0x80000000:
                        raw_val -= 0x100000000
                except ValueError:
                    continue

                self._event_count += 1

                # EV_REL = 0x0002
                if type_val == 0x0002:
                    if code_val == 0x0000:  # REL_X
                        accumulated_dx += raw_val * self._sensitivity
                    elif code_val == 0x0001:  # REL_Y
                        accumulated_dy += raw_val * self._sensitivity
                    elif code_val == 0x0008:  # REL_WHEEL
                        input_service.mouse_scroll(0, raw_val)

                # EV_SYN = 0x0000 (SYN_REPORT)
                elif type_val == 0x0000:
                    int_dx = int(accumulated_dx)
                    int_dy = int(accumulated_dy)
                    if int_dx != 0 or int_dy != 0:
                        input_service.mouse_move(int_dx, int_dy)
                        accumulated_dx -= int_dx
                        accumulated_dy -= int_dy
                        self._last_event_text = f"MOVE: ({int_dx}, {int_dy})"

                # EV_KEY = 0x0001
                elif type_val == 0x0001:
                    is_down = raw_val == 1
                    btn_map = {
                        0x0110: "left",
                        0x0111: "right",
                        0x0112: "middle",
                        0x0113: "back",
                        0x0114: "forward",
                    }
                    if code_val in btn_map:
                        btn_name = btn_map[code_val]
                        if is_down:
                            input_service.mouse_down(btn_name)
                        else:
                            input_service.mouse_up(btn_name)
                        self._last_event_text = (
                            f"BUTTON: {btn_name} {'DOWN' if is_down else 'UP'}"
                        )

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Error in ADB stream loop: {e}", exc_info=True)
        finally:
            self._process = None


adb_hub_service = AdbHubService()
