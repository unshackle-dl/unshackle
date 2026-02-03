"""
MonaLisa CDM - WASM-based Content Decryption Module wrapper.

This module provides key extraction from MonaLisa-protected content using
a WebAssembly module that runs locally via wasmtime.
"""

import base64
import ctypes
import json
import re
import uuid
from pathlib import Path
from typing import Dict, Optional, Union

import wasmtime

from unshackle.core import binaries


class MonaLisaCDM:
    """
    MonaLisa CDM wrapper for WASM-based key extraction.

    This CDM differs from Widevine/PlayReady in that it does not use a
    challenge/response flow with a license server. Instead, the license
    (ticket) is provided directly by the service API, and keys are extracted
    locally via the WASM module.
    """

    DYNAMIC_BASE = 6065008
    DYNAMICTOP_PTR = 821968
    LICENSE_KEY_OFFSET = 0x5C8C0C
    LICENSE_KEY_LENGTH = 16

    ENV_STRINGS = (
        "USER=web_user",
        "LOGNAME=web_user",
        "PATH=/",
        "PWD=/",
        "HOME=/home/web_user",
        "LANG=zh_CN.UTF-8",
        "_=./this.program",
    )

    def __init__(self, device_path: Path):
        """
        Initialize the MonaLisa CDM.

        Args:
            device_path: Path to the device file (.mld).
        """
        device_path = Path(device_path)

        self.device_path = device_path
        self.base_dir = device_path.parent

        if not self.device_path.is_file():
            raise FileNotFoundError(f"Device file not found at: {self.device_path}")

        try:
            data = json.loads(self.device_path.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            raise ValueError(f"Invalid device file (JSON): {e}")

        wasm_path_str = data.get("wasm_path")
        if not wasm_path_str:
            raise ValueError("Device file missing 'wasm_path'")

        wasm_filename = Path(wasm_path_str).name
        wasm_path = self.base_dir / wasm_filename

        if not wasm_path.exists():
            raise FileNotFoundError(f"WASM file not found at: {wasm_path}")

        try:
            self.engine = wasmtime.Engine()
            if wasm_path.suffix.lower() == ".wat":
                self.module = wasmtime.Module.from_file(self.engine, str(wasm_path))
            else:
                self.module = wasmtime.Module(self.engine, wasm_path.read_bytes())
        except Exception as e:
            raise RuntimeError(f"Failed to load WASM module: {e}")

        self.store = None
        self.memory = None
        self.instance = None
        self.exports = {}
        self.ctx = None

    @staticmethod
    def get_worker_path() -> Optional[Path]:
        """Get ML-Worker binary path from the unshackle binaries system."""
        if binaries.ML_Worker:
            return Path(binaries.ML_Worker)
        return None

    def open(self) -> int:
        """
        Open a CDM session.

        Returns:
            Session ID (always 1 for MonaLisa).

        Raises:
            RuntimeError: If session initialization fails.
        """
        try:
            self.store = wasmtime.Store(self.engine)
            memory_type = wasmtime.MemoryType(wasmtime.Limits(256, 256))
            self.memory = wasmtime.Memory(self.store, memory_type)

            self._write_i32(self.DYNAMICTOP_PTR, self.DYNAMIC_BASE)
            imports = self._build_imports()
            self.instance = wasmtime.Instance(self.store, self.module, imports)

            ex = self.instance.exports(self.store)
            self.exports = {
                "___wasm_call_ctors": ex["s"],
                "_monalisa_context_alloc": ex["D"],
                "monalisa_set_license": ex["F"],
                "_monalisa_set_canvas_id": ex["t"],
                "_monalisa_version_get": ex["A"],
                "monalisa_get_line_number": ex["v"],
                "stackAlloc": ex["N"],
                "stackSave": ex["L"],
                "stackRestore": ex["M"],
            }

            self.exports["___wasm_call_ctors"](self.store)
            self.ctx = self.exports["_monalisa_context_alloc"](self.store)
            return 1
        except Exception as e:
            raise RuntimeError(f"Failed to initialize session: {e}")

    def close(self, session_id: int = 1) -> None:
        """
        Close the CDM session and release resources.

        Args:
            session_id: The session ID to close (unused, for API compatibility).
        """
        self.store = None
        self.memory = None
        self.instance = None
        self.exports = {}
        self.ctx = None

    def extract_keys(self, license_data: Union[str, bytes]) -> Dict:
        """
        Extract decryption keys from license/ticket data.

        Args:
            license_data: The license ticket, either as base64 string or raw bytes.

        Returns:
            Dictionary with keys: kid (hex), key (hex), type ("CONTENT").

        Raises:
            RuntimeError: If session not open or license validation fails.
            ValueError: If license_data is empty.
        """
        if not self.instance or not self.memory or self.ctx is None:
            raise RuntimeError("Session not open. Call open() first.")

        if not license_data:
            raise ValueError("license_data is empty")

        if isinstance(license_data, bytes):
            license_b64 = base64.b64encode(license_data).decode("utf-8")
        else:
            license_b64 = license_data

        ret = self._ccall(
            "monalisa_set_license",
            int,
            self.ctx,
            license_b64,
            len(license_b64),
            "0",
        )

        if ret != 0:
            raise RuntimeError(f"License validation failed with code: {ret}")

        key_bytes = self._extract_license_key_bytes()

        # Extract DCID from license to generate KID
        try:
            decoded = base64.b64decode(license_b64).decode("ascii", errors="ignore")
        except Exception:
            decoded = ""

        m = re.search(
            r"DCID-[A-Z0-9]+-[A-Z0-9]+-\d{8}-\d{6}-[A-Z0-9]+-\d{10}-[A-Z0-9]+",
            decoded,
        )
        if m:
            kid_bytes = uuid.uuid5(uuid.NAMESPACE_DNS, m.group()).bytes
        else:
            kid_bytes = uuid.UUID(int=0).bytes

        return {"kid": kid_bytes.hex(), "key": key_bytes.hex(), "type": "CONTENT"}

    def _extract_license_key_bytes(self) -> bytes:
        """Extract the 16-byte decryption key from WASM memory."""
        data_ptr = self.memory.data_ptr(self.store)
        data_len = self.memory.data_len(self.store)

        if self.LICENSE_KEY_OFFSET + self.LICENSE_KEY_LENGTH > data_len:
            raise RuntimeError("License key offset beyond memory bounds")

        mem_ptr = ctypes.cast(data_ptr, ctypes.POINTER(ctypes.c_ubyte * data_len))
        start = self.LICENSE_KEY_OFFSET
        end = self.LICENSE_KEY_OFFSET + self.LICENSE_KEY_LENGTH

        return bytes(mem_ptr.contents[start:end])

    def _ccall(self, func_name: str, return_type: type, *args):
        """Call a WASM function with automatic string conversion."""
        stack = 0
        converted_args = []

        for arg in args:
            if isinstance(arg, str):
                if stack == 0:
                    stack = self.exports["stackSave"](self.store)
                max_length = (len(arg) << 2) + 1
                ptr = self.exports["stackAlloc"](self.store, max_length)
                self._string_to_utf8(arg, ptr, max_length)
                converted_args.append(ptr)
            else:
                converted_args.append(arg)

        result = self.exports[func_name](self.store, *converted_args)

        if stack != 0:
            self.exports["stackRestore"](self.store, stack)

        if return_type is bool:
            return bool(result)
        return result

    def _write_i32(self, addr: int, value: int) -> None:
        """Write a 32-bit integer to WASM memory."""
        data = self.memory.data_ptr(self.store)
        mem_ptr = ctypes.cast(data, ctypes.POINTER(ctypes.c_int32))
        mem_ptr[addr >> 2] = value

    def _string_to_utf8(self, data: str, ptr: int, max_length: int) -> int:
        """Convert string to UTF-8 and write to WASM memory."""
        encoded = data.encode("utf-8")
        write_length = min(len(encoded), max_length - 1)

        mem_data = self.memory.data_ptr(self.store)
        mem_ptr = ctypes.cast(mem_data, ctypes.POINTER(ctypes.c_ubyte))

        for i in range(write_length):
            mem_ptr[ptr + i] = encoded[i]
        mem_ptr[ptr + write_length] = 0
        return write_length

    def _write_ascii_to_memory(self, string: str, buffer: int, dont_add_null: int = 0) -> None:
        """Write ASCII string to WASM memory."""
        mem_data = self.memory.data_ptr(self.store)
        mem_ptr = ctypes.cast(mem_data, ctypes.POINTER(ctypes.c_ubyte))

        encoded = string.encode("utf-8")
        for i, byte_val in enumerate(encoded):
            mem_ptr[buffer + i] = byte_val

        if dont_add_null == 0:
            mem_ptr[buffer + len(encoded)] = 0

    def _build_imports(self):
        """Build the WASM import stubs required by the MonaLisa module."""

        def sys_fcntl64(a, b, c):
            return 0

        def fd_write(a, b, c, d):
            return 0

        def fd_close(a):
            return 0

        def sys_ioctl(a, b, c):
            return 0

        def sys_open(a, b, c):
            return 0

        def sys_rmdir(a):
            return 0

        def sys_unlink(a):
            return 0

        def clock():
            return 0

        def time(a):
            return 0

        def emscripten_run_script(a):
            return None

        def fd_seek(a, b, c, d, e):
            return 0

        def emscripten_resize_heap(a):
            return 0

        def fd_read(a, b, c, d):
            return 0

        def emscripten_run_script_string(a):
            return 0

        def emscripten_run_script_int(a):
            return 1

        def emscripten_memcpy_big(dest, src, num):
            mem_data = self.memory.data_ptr(self.store)
            data_len = self.memory.data_len(self.store)
            if num is None:
                num = data_len - 1
            mem_ptr = ctypes.cast(mem_data, ctypes.POINTER(ctypes.c_ubyte))
            for i in range(num):
                if dest + i < data_len and src + i < data_len:
                    mem_ptr[dest + i] = mem_ptr[src + i]
            return dest

        def environ_get(environ_ptr, environ_buf):
            buf_size = 0
            for index, string in enumerate(self.ENV_STRINGS):
                ptr = environ_buf + buf_size
                self._write_i32(environ_ptr + index * 4, ptr)
                self._write_ascii_to_memory(string, ptr)
                buf_size += len(string) + 1
            return 0

        def environ_sizes_get(penviron_count, penviron_buf_size):
            self._write_i32(penviron_count, len(self.ENV_STRINGS))
            buf_size = sum(len(s) + 1 for s in self.ENV_STRINGS)
            self._write_i32(penviron_buf_size, buf_size)
            return 0

        i32 = wasmtime.ValType.i32()

        return [
            wasmtime.Func(self.store, wasmtime.FuncType([i32, i32, i32], [i32]), sys_fcntl64),
            wasmtime.Func(self.store, wasmtime.FuncType([i32, i32, i32, i32], [i32]), fd_write),
            wasmtime.Func(self.store, wasmtime.FuncType([i32], [i32]), fd_close),
            wasmtime.Func(self.store, wasmtime.FuncType([i32, i32, i32], [i32]), sys_ioctl),
            wasmtime.Func(self.store, wasmtime.FuncType([i32, i32, i32], [i32]), sys_open),
            wasmtime.Func(self.store, wasmtime.FuncType([i32], [i32]), sys_rmdir),
            wasmtime.Func(self.store, wasmtime.FuncType([i32], [i32]), sys_unlink),
            wasmtime.Func(self.store, wasmtime.FuncType([], [i32]), clock),
            wasmtime.Func(self.store, wasmtime.FuncType([i32], [i32]), time),
            wasmtime.Func(self.store, wasmtime.FuncType([i32], []), emscripten_run_script),
            wasmtime.Func(self.store, wasmtime.FuncType([i32, i32, i32, i32, i32], [i32]), fd_seek),
            wasmtime.Func(self.store, wasmtime.FuncType([i32, i32, i32], [i32]), emscripten_memcpy_big),
            wasmtime.Func(self.store, wasmtime.FuncType([i32], [i32]), emscripten_resize_heap),
            wasmtime.Func(self.store, wasmtime.FuncType([i32, i32], [i32]), environ_get),
            wasmtime.Func(self.store, wasmtime.FuncType([i32, i32], [i32]), environ_sizes_get),
            wasmtime.Func(self.store, wasmtime.FuncType([i32, i32, i32, i32], [i32]), fd_read),
            wasmtime.Func(self.store, wasmtime.FuncType([i32], [i32]), emscripten_run_script_string),
            wasmtime.Func(self.store, wasmtime.FuncType([i32], [i32]), emscripten_run_script_int),
            self.memory,
        ]
