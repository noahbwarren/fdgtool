#!/usr/bin/env python3
"""Keyence FD-G terminal configuration tool. Python 3.9+, standard library only.

Addresses shown to the operator are the manual's ZERO-BASED relative addresses.
Only Modpoll sees address + 1, with -0 deliberately omitted for Modpoll 3.4.
"""

import argparse
from collections import deque
import csv
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional


VERSION = "1.1.0"
DEFAULT_HOST = os.environ.get("FDG_HOST", "127.0.0.1")
RESOLUTION = {0: Decimal("0.001"), 1: Decimal("0.01"),
              2: Decimal("0.1"), 3: Decimal("1")}
PRESSURE = {0: (Decimal("0.1"), "kPa"), 1: (Decimal("0.001"), "bar"),
            2: (Decimal("0.01"), "psi")}
PIPE_SIZES = {
    "FD-G25": {0: "--", 1: '1"', 2: '3/4"'},
    "FD-G50": {0: "--", 1: '2"', 2: '1 1/2"', 3: '1 1/4"'},
    "FD-G80": {0: "--", 1: '3"', 2: '2 1/2"'},
    "FD-G125": {0: "--", 1: '5"', 2: '4"'},
    "FD-G200": {0: "--", 1: '8"', 2: '6"'},
}
WARNINGS = ["Head error", "Overcurrent error", "Memory error", "Input pressure error",
            "Pulse error", "Clock error", "Response time error", "Detection error",
            "Reverse flow", "IP duplication error", "Network error"]
STABILITY = {0: "Detection not possible", 1: "Low", 2: "Medium-low",
             3: "Medium-high", 4: "High"}
CONFIG_RANGES = [(25, 2), (100, 3), (106, 8), (116, 1), (194, 8), (207, 3)]
STATUS_RANGES = [(0, 2), (4, 13), (22, 5), (116, 1), (194, 8), (207, 3)]


class ToolError(Exception):
    pass


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def decimal_text(value):
    return format(value, "f")


def u32(words, address):
    return words[address] | (words[address + 1] << 16)


def i32(words, address):
    value = u32(words, address)
    return value - (1 << 32) if value & (1 << 31) else value


def split_u32(value):
    if not 0 <= value <= 0xFFFFFFFF:
        raise ToolError("Value does not fit an unsigned 32-bit register pair.")
    return [value & 0xFFFF, value >> 16]


def label(choices, value):
    return choices.get(value, "Unknown ({})".format(value))


def normalize(value):
    return re.sub(r"[\s_()-]+", "", value.casefold())


def model_name(value):
    model = value.upper().replace("(L)", "").rstrip("L")
    if model not in PIPE_SIZES:
        raise argparse.ArgumentTypeError("Choose " + ", ".join(PIPE_SIZES))
    return model


@dataclass(frozen=True)
class Field:
    key: str
    address: int
    title: str
    minimum: int = 0
    maximum: int = 65535
    width: int = 1
    choices: Optional[Dict[int, str]] = None
    scale: str = "1"
    unit: str = ""
    writable: bool = True
    note: str = ""

    @property
    def registers(self):
        return str(self.address) if self.width == 1 else "{}-{}".format(
            self.address, self.address + self.width - 1)

    def raw_value(self, words):
        return words[self.address] if self.width == 1 else u32(words, self.address)

    def enum(self, model=None):
        if self.address == 196:
            return PIPE_SIZES.get(model)
        return self.choices

    def measure(self, words, flow_mode="resolution"):
        if self.address == 101:
            if flow_mode == "integer":
                return Decimal(1), unit_labels(words)[0]
            if words[25] not in RESOLUTION:
                raise ToolError("Unknown flow resolution; use raw:<integer> or --raw for zero cut.")
            return RESOLUTION[words[25]], unit_labels(words)[0]
        if self.address == 201:
            if words[208] not in PRESSURE:
                raise ToolError("Unknown pressure unit; use --raw or correct register 208.")
            return PRESSURE[words[208]]
        if self.address == 116:
            if words[209] not in (0, 1):
                raise ToolError("Unknown temperature unit; use raw:<integer> or --raw.")
            return Decimal("0.1"), label({0: "degC", 1: "degF"}, words[209])
        return Decimal(self.scale), self.unit

    def display(self, words, model=None, flow_mode="resolution"):
        raw = self.raw_value(words)
        options = self.enum(model)
        if options is not None:
            return "{} (code {})".format(label(options, raw), raw)
        if self.address == 196:
            return "code {} (size variant not selected)".format(raw)
        try:
            scale, unit = self.measure(words, flow_mode)
        except ToolError:
            return "{} raw (unknown unit)".format(raw)
        return "{} {}".format(decimal_text(Decimal(raw) * scale), unit).strip()

    def parse(self, text, words, model=None, raw_mode=False, flow_mode="resolution"):
        text = text.strip()
        if not self.writable:
            raise ToolError("Registers {} are read-only in this manual.".format(self.registers))
        if self.address == 196 and model is None:
            raise ToolError("Select the FD-G size variant with --model or menu option m first.")
        options = self.enum(model)
        if options and not raw_mode:
            for code, name in options.items():
                if normalize(text) == normalize(name):
                    return code
        try:
            number = Decimal(int(text, 16)) if text.lower().startswith("0x") else Decimal(text)
        except (ValueError, InvalidOperation):
            raise ToolError("Enter a listed code/label or a numeric value.")
        if not number.is_finite():
            raise ToolError("Value must be finite.")
        scale = Decimal(1) if raw_mode or options else self.measure(words, flow_mode)[0]
        raw = number / scale
        if raw != raw.to_integral_value():
            places = max(0, -scale.as_tuple().exponent)
            raise ToolError("Use up to {} decimal place(s); the smallest step is {}.".format(places, scale))
        raw = int(raw)
        if options is not None and raw not in options:
            raise ToolError("Allowed codes: " + ", ".join(map(str, options)))
        if not self.minimum <= raw <= self.maximum:
            if raw_mode or options:
                raise ToolError("Allowed integer range: {} to {}.".format(self.minimum, self.maximum))
            raise ToolError("Allowed range: {} to {} {}.".format(
                Decimal(self.minimum) * scale, Decimal(self.maximum) * scale,
                self.measure(words, flow_mode)[1]))
        return raw


FIELDS = [
    Field("flow_resolution", 25, "Flow resolution", 0, 3,
          choices={0: "0.001", 1: "0.01", 2: "0.1", 3: "1"}, writable=False),
    Field("total_resolution", 26, "Integrated flow resolution", 0, 3,
          choices={0: "0.001", 1: "0.01", 2: "0.1", 3: "1"}, writable=False),
    Field("response_time", 100, "Response time", 0, 7,
          choices=dict(enumerate(["1s", "2.5s", "5s", "10s", "30s", "60s", "120s", "200s"]))),
    Field("zero_cut", 101, "Zero cut flow rate", 0, 999999, width=2,
          note="Enter flow in the current unit. Resolution mode uses register 25; integer mode uses whole units. "
               "Use explicit raw input if the flow decimal interpretation has not been confirmed."),
    Field("pipe_material", 106, "Pipe material", 0, 1,
          choices={0: "Steel", 1: "Stainless steel (SUS)"}),
    Field("advanced_pipe", 107, "Advanced pipe settings", 0, 1, choices={0: "OFF", 1: "ON"}),
    Field("pipe_od", 108, "Pipe outer diameter", 1, 30000, scale="0.01", unit="mm",
          note="Enabled only when advanced pipe settings (107) are ON."),
    Field("pipe_thickness", 109, "Pipe thickness", 10, 3000, scale="0.01", unit="mm",
          note="Enabled only when advanced pipe settings (107) are ON."),
    Field("gas_type", 110, "Gas type", 0, 1, choices={0: "Air", 1: "Input value"}),
    Field("specific_heat_ratio", 111, "Specific heat ratio", 1000, 2000, scale="0.001",
          note="Used with Gas type = Input value; Air uses the meter's internal gas properties."),
    Field("molecular_weight", 112, "Average molecular weight", 100, 10000, scale="0.01",
          note="Used with Gas type = Input value."),
    Field("span", 113, "Span adjustment", 100, 10000, scale="0.001", unit="x"),
    Field("reference_temp", 116, "Conversion reference temperature", 0, 1000,
          note="Temperature is in the currently selected unit; used for STP conversion."),
    Field("integration_direction", 194, "Integration direction", 0, 1,
          choices={0: "Standard", 1: "Bi-directional"},
          note="Bi-directional mode also limits the meter's leakage, target and I/O functions (manual p28)."),
    Field("flow_direction", 195, "Flow direction", 0, 1,
          choices={0: "To the cable connector", 1: "From the cable connector"}),
    Field("pipe_size", 196, "Pipe size", 0, 3,
          note="Code meanings depend on size variant. Pipe size/standard can reset pipe dimensions."),
    Field("piping_standard", 197, "Piping standard", 0, 5,
          choices={0: "--", 1: "JIS", 2: "ASTM", 3: "DIN", 4: "GB", 5: "Other"},
          note="Selecting pipe size/standard can automatically update pipe outer diameter and thickness."),
    Field("conversion", 198, "Conversion settings", 0, 2,
          choices={0: "NTP", 1: "STP", 2: "OFF"},
          note="NTP: 0 degC / 101.3 kPa. STP: reference temperature / 101.3 kPa. OFF: actual volume."),
    Field("flow_unit", 199, "Flow unit", 0, 2, choices={0: "m3/h", 1: "L/min", 2: "CFM"},
          note="Changes the meter's unit. Recheck resolution and unit-dependent settings afterward."),
    Field("pressure_input", 200, "Pressure input", 0, 2,
          choices={0: "--", 1: "Manual input", 2: "Analog input"},
          note="Analog scaling registers 202-203 are outside this tool's editable set."),
    Field("pipe_pressure", 201, "Pipe internal pressure", 0, 60000,
          note="Enter pressure in the current unit (208); used with Manual input."),
    Field("additional_unit", 207, "Add. meas. Unit", 0, 1, choices={0: "ON", 1: "OFF"},
          note="The manual's ON/OFF codes are 0=ON and 1=OFF for this setting."),
    Field("pressure_unit", 208, "Pressure unit", 0, 2, choices={0: "kPa", 1: "bar", 2: "psi"},
          note="Recheck pressure values after changing the unit."),
    Field("temperature_unit", 209, "Temperature unit", 0, 1, choices={0: "C", 1: "F"},
          note="Recheck conversion reference temperature after changing the unit."),
]
BY_KEY = {f.key: f for f in FIELDS}
BY_ADDRESS = {f.address: f for f in FIELDS}


def find_field(value):
    value = value.strip().lower()
    if value in BY_KEY:
        return BY_KEY[value]
    for field in FIELDS:
        names = (field.key, field.title, SHORT_TITLES.get(field.key, ""))
        if any(name and normalize(value) == normalize(name) for name in names):
            return field
    try:
        address = int(value)
    except ValueError:
        raise ToolError("Unknown setting. Use settings or catalog to list names and addresses.")
    if address == 102:
        raise ToolError("102 is the upper word of zero_cut. Edit 101 (the complete 101-102 pair).")
    if address not in BY_ADDRESS:
        raise ToolError("Address {} is outside the supported settings.".format(address))
    return BY_ADDRESS[address]


class Modpoll:
    def __init__(self, host, port=502, unit_id=1, executable="modpoll", timeout=2.0,
                 runner=subprocess.run):
        if not host or host.startswith("-") or any(c.isspace() for c in host):
            raise ToolError("Host must be an IP address or hostname.")
        self.host, self.port, self.unit_id = host, port, unit_id
        self.executable, self.timeout, self.runner = executable, timeout, runner
        self.demo = False

    def command(self, address, count=None, values=None):
        # No -0: older Modpoll rejects -r 0. PDU n is reference n+1.
        args = [self.executable, "-m", "tcp", "-a", str(self.unit_id),
                "-p", str(self.port), "-t", "4:hex" if values is None else "4",
                "-r", str(address + 1), "-1", "-o", str(self.timeout)]
        if values is None:
            args.extend(["-c", str(count)])
        args.append(self.host)
        if values is not None:
            # Modpoll writes consecutive 16-bit words; pairs use one request.
            args.extend(str(value) for value in values)
        return args

    def run(self, args):
        try:
            result = self.runner(args, capture_output=True, text=True,
                                 timeout=self.timeout + 5, check=False)
        except FileNotFoundError:
            raise ToolError("Modpoll not found. Supply its path with --modpoll.")
        except subprocess.TimeoutExpired:
            raise ToolError("Modpoll process timed out.")
        except OSError as exc:
            raise ToolError("Could not run Modpoll: {}".format(exc))
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.returncode:
            raise ToolError("Modpoll exited {}:\n{}".format(result.returncode, output.strip()))
        return output

    def read(self, address, count):
        output = self.run(self.command(address, count=count))
        rows = {}
        for line in output.splitlines():
            match = re.fullmatch(r"\s*\[(\d+)\]:\s*(0x[0-9a-fA-F]+|[+-]?\d+)\s*", line)
            if match:
                reference, token = int(match[1]), match[2]
                value = int(token, 16) if token.lower().startswith("0x") else int(token)
                if reference in rows or not -32768 <= value <= 65535:
                    raise ToolError("Invalid or duplicated register row in Modpoll output.")
                rows[reference] = value & 0xFFFF
        expected = set(range(address + 1, address + count + 1))
        if set(rows) != expected:
            raise ToolError("Incomplete/unexpected Modpoll read for registers {}-{}:\n{}".format(
                address, address + count - 1, output.strip()))
        return {reference - 1: word for reference, word in rows.items()}

    def write(self, address, values):
        self.run(self.command(address, values=values))


class Demo:
    """Offline demonstration; no Modpoll subprocesses or network connections."""
    demo = True
    host, port, unit_id = "DEMO", 502, 1

    def __init__(self):
        self.words = {i: 0 for start, count in CONFIG_RANGES + STATUS_RANGES
                      for i in range(start, start + count)}
        self.words.update({0: 1, 1: 0x100, 4: 0xB7A4, 5: 0xFFFF, 6: 12000,
                           8: 0xB7A4, 9: 0xFFFF, 10: 12345, 12: 25, 14: 4,
                           16: 5000, 25: 1, 26: 1, 100: 3, 101: 180,
                           107: 1, 108: 6030, 109: 392, 111: 1403, 112: 2897,
                           113: 1000, 116: 200, 196: 1, 197: 2, 198: 1,
                           200: 1, 201: 5000})

    def read(self, address, count):
        return {i: self.words[i] for i in range(address, address + count)}

    def write(self, address, values):
        self.words.update({address + offset: value for offset, value in enumerate(values)})


def read_ranges(client, ranges):
    result = {}
    for start, count in ranges:
        result.update(client.read(start, count))
    return result


def read_config(client):
    return read_ranges(client, CONFIG_RANGES)


def validate_change(field, raw, words):
    proposed = dict(words)
    values = split_u32(raw) if field.width == 2 else [raw]
    proposed.update({field.address + index: value for index, value in enumerate(values)})
    if field.address in (108, 109) and words[107] != 1:
        raise ToolError("Set advanced_pipe (107) to ON before editing pipe dimensions.")
    if field.address in (107, 108, 109) and proposed[107] == 1:
        if proposed[108] <= 2 * proposed[109]:
            raise ToolError("Pipe outer diameter must exceed twice its wall thickness.")
    return proposed


class Journal:
    def __init__(self, directory, client, flow_mode="resolution"):
        self.directory = Path(directory)
        self.client = client
        self.flow_mode = flow_mode

    def metadata(self):
        return {"timestamp_utc": stamp(), "host": self.client.host,
                "port": self.client.port, "unit_id": self.client.unit_id,
                "demo": self.client.demo, "tool_version": VERSION,
                "flow_mode": self.flow_mode,
                "addressing": "manual zero-based relative addresses"}

    def snapshot(self, words, model=None, purpose="manual"):
        self.directory.mkdir(parents=True, exist_ok=True)
        name = "{}-{}-{}.json".format(
            "demo" if self.client.demo else "fdg",
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"), purpose)
        path = self.directory / name
        data = self.metadata()
        data.update({"purpose": purpose, "model": model, "registers": words,
                     "settings": {f.key: {"registers": f.registers,
                                          "raw": f.raw_value(words),
                                          "display": f.display(words, model, self.flow_mode),
                                          "writable": f.writable} for f in FIELDS}})
        with path.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        return path

    def event(self, event, **details):
        self.directory.mkdir(parents=True, exist_ok=True)
        record = self.metadata()
        record.update({"event": event, **details})
        with (self.directory / "changes.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def edit_setting(client, journal, field, value, model=None, raw_mode=False,
                 confirm=input, out=print, before=None, ui=None):
    # Reject read-only/unknown words before connecting to the device.
    if not field.writable:
        raise ToolError("Registers {} are read-only; they are displayed only.".format(field.registers))
    before = dict(before) if before is not None else read_config(client)
    mode = journal.flow_mode
    raw = field.parse(value, before, model, raw_mode, mode)
    proposed = validate_change(field, raw, before)
    old = field.raw_value(before)
    if ui:
        ui.box("REVIEW CHANGE", [
            "Meter   {} | register {}".format(client.host, field.registers),
            "Setting {}".format(field.title),
            "Current {}".format(field.display(before, model, mode)),
            ("New     {}".format(field.display(proposed, model, mode)), "accent"),
            "Encoded register integer: {} -> {}".format(old, raw)])
    else:
        out("{} [{}] {}: {} -> {}".format(client.host, field.registers, field.title,
            field.display(before, model, mode), field.display(proposed, model, mode)))
        out("Raw integer: {} -> {}".format(old, raw))
    if field.note:
        out(field.note)
    if raw == old:
        out("Already set; no write needed.")
        return False
    if confirm("Type APPLY to write this setting, or Enter to cancel: ").strip() != "APPLY":
        out("Cancelled.")
        return False
    # A unit/model dependency or another client changing configuration invalidates the preview.
    if read_config(client) != before:
        raise ToolError("Settings changed since the preview. Read them again before editing.")
    backup = journal.snapshot(before, model, "before")
    journal.event("write_attempt", field=field.key, registers=field.registers,
                  old_raw=old, requested_raw=raw, snapshot=str(backup))
    values = split_u32(raw) if field.width == 2 else [raw]
    write_error = None
    try:
        client.write(field.address, values)
    except (ToolError, KeyboardInterrupt) as exc:
        write_error = str(exc) or "Interrupted during write"
    # Always inspect read-back, even if the response to the write was lost. Never retry writes.
    try:
        actual = field.raw_value(client.read(field.address, field.width))
    except (ToolError, KeyboardInterrupt) as exc:
        journal.event("write_outcome_unknown", field=field.key,
                      error=str(exc), write_error=write_error, snapshot=str(backup))
        raise ToolError("Write outcome UNKNOWN. No retry was attempted.\nSnapshot: {}\n{}".format(backup, exc))
    if actual != raw:
        journal.event("write_not_verified", field=field.key, requested_raw=raw,
                      actual_raw=actual, write_error=write_error, snapshot=str(backup))
        raise ToolError("Read-back mismatch: requested {}, read {}. No rollback/retry was attempted.\n"
                        "Snapshot: {}{}".format(raw, actual, backup,
                                               "\n" + write_error if write_error else ""))
    journal.event("write_verified", field=field.key, actual_raw=actual,
                  write_error=write_error, snapshot=str(backup))
    out("Verified read-back. Previous settings: {}".format(backup))
    if write_error:
        out("The write command reported an error, but read-back matches: " + write_error)
    # Capture automatic changes to dimensions, units, resolutions, etc.
    try:
        after = read_config(client)
        post = journal.snapshot(after, model, "after")
        for other in FIELDS:
            if other.key != field.key and other.raw_value(before) != other.raw_value(after):
                out("Also changed [{}] {}: {} -> {}".format(other.registers, other.title,
                    other.display(before, model, mode), other.display(after, model, mode)))
        out("Updated settings: {}".format(post))
    except (ToolError, OSError) as exc:
        out("Target value was verified; the full settings refresh failed: {}".format(exc))
    return True


def unit_labels(words):
    suffix = {0: "(N)", 1: "(S)", 2: ""}.get(words[198], "(?)") if words[207] == 0 else ""
    flow = label({0: "m3/h", 1: "L/min", 2: "CFM"}, words[199]) + suffix
    total = label({0: "m3", 1: "L", 2: "ft3"}, words[199]) + suffix
    return flow, total


def flow_value(raw, code, mode):
    if mode == "integer":
        return str(raw)
    if code not in RESOLUTION:
        return "unavailable (unknown resolution code {})".format(code)
    return decimal_text(Decimal(raw) * RESOLUTION[code])


def warning_names(mask):
    result = [name for bit, name in enumerate(WARNINGS) if mask & (1 << bit)]
    unknown = mask & ~((1 << len(WARNINGS)) - 1)
    if unknown:
        result.append("Unknown bits 0x{:04X}".format(unknown))
    return "; ".join(result) or "None"


def measurements(words, mode):
    flow_unit, total_unit = unit_labels(words)
    pressure_scale, pressure_unit = PRESSURE.get(words[208], (Decimal(1), "raw (unknown unit)"))
    return {"timestamp_utc": stamp(), "flow_raw": i32(words, 4),
            "flow_display": flow_value(i32(words, 4), words[25], mode),
            "flow_mode": mode, "flow_unit": flow_unit,
            "flow_resolution_code": words[25], "total_resolution_code": words[26],
            "conversion": label({0: "NTP", 1: "STP", 2: "OFF"}, words[198]),
            "total_raw": u32(words, 10),
            "total_display": flow_value(u32(words, 10), words[26], mode),
            "total_unit": total_unit,
            "pressure": decimal_text(Decimal(words[16]) * pressure_scale),
            "pressure_unit": pressure_unit, "stability": label(STABILITY, words[14]),
            "initialized": words[0], "error_mask": "0x{:04X}".format(words[1]),
            "warnings": warning_names(words[1]), "error": ""}


class Terminal:
    """Small, dependency-free ANSI interface with plain-output fallbacks."""
    COLORS = {"accent": "1;36", "title": "1;37", "good": "1;32",
              "warning": "1;33", "error": "1;31", "dim": "2;37", "normal": "0"}

    def __init__(self, color="auto", ascii_only=False, out=print, width=None,
                 height=None, interactive=None):
        size = shutil.get_terminal_size((100, 30))
        self.width = max(24, min(width or size.columns, 120))
        self.height = height or size.lines
        self.out = out
        self.interactive = (sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
                            if interactive is None else interactive)
        self.color = color == "always" or (color == "auto" and self.interactive
                                           and "NO_COLOR" not in os.environ)
        try:
            "╭─╮│╰╯▁█".encode(sys.stdout.encoding or "ascii")
            self.unicode = self.interactive and not ascii_only
        except UnicodeEncodeError:
            self.unicode = False

    def style(self, text, role="normal"):
        if not self.color:
            return str(text)
        return "\033[{}m{}\033[0m".format(self.COLORS.get(role, "0"), text)

    def line(self, text="", role="normal"):
        self.out(self.style(text, role))

    def clear(self):
        if self.interactive:
            self.control("\033[2J\033[H")

    def control(self, sequence):
        if self.out is print:
            print(sequence, end="", flush=True)
        else:
            self.out(sequence)

    def cursor(self, visible):
        if self.interactive:
            self.control("\033[?25h" if visible else "\033[?25l")

    @staticmethod
    def fit(text, width):
        text = str(text)
        if len(text) > width:
            return text[:max(0, width - 2)] + ".."
        return text.ljust(width)

    def box(self, title, lines, role="accent"):
        inner = self.width - 4
        left, top, right, side, bottom_left, bottom_right = (
            ("╭", "─", "╮", "│", "╰", "╯") if self.unicode else ("+", "-", "+", "|", "+", "+"))
        heading = (" " + title + " ")[:self.width - 2]
        self.line(left + heading + top * (self.width - 2 - len(heading)) + right, role)
        for row in lines:
            text, row_role = row if isinstance(row, tuple) else (str(row), "normal")
            for part in str(text).splitlines() or [""]:
                for wrapped in textwrap.wrap(part, width=inner, replace_whitespace=False,
                                              drop_whitespace=False) or [""]:
                    self.line(side + " " + wrapped.ljust(inner) + " " + side, row_role)
        self.line(bottom_left + top * (self.width - 2) + bottom_right, role)

    def banner(self, client, section="DASHBOARD"):
        self.box("FD-G  /  FIELD CONSOLE", [
            ("{}   |   v{}{}".format(section, VERSION,
             "   |   DEMO / SIMULATED" if client.demo else ""), "accent"),
            "{}:{}  /  Unit ID {}".format(client.host, client.port, client.unit_id)])

    def cards(self, cards):
        if self.width < 72:
            for title, value, unit, role in cards:
                self.box(title, [(value + " " + unit, role)], role)
            return
        sizes = [(self.width - 2) // 3] * 3
        sizes[-1] += self.width - 2 - sum(sizes)
        rows = [[] for _ in range(4)]
        for size, (title, value, unit, role) in zip(sizes, cards):
            left, top, right, side, bl, br = (
                ("╭", "─", "╮", "│", "╰", "╯") if self.unicode else ("+", "-", "+", "|", "+", "+"))
            heading = " " + title + " "
            cells = [left + heading + top * (size - 2 - len(heading)) + right,
                     side + " " + self.fit(value, size - 4) + " " + side,
                     side + " " + self.fit(unit, size - 4) + " " + side,
                     bl + top * (size - 2) + br]
            for index, cell in enumerate(cells):
                rows[index].append(self.style(cell, role if index == 1 else "accent"))
        for row in rows:
            self.out(" ".join(row))

    def message(self, text, role="normal"):
        for part in textwrap.wrap(str(text), self.width) or [""]:
            self.line(part, role)


def pretty_number(value):
    try:
        return format(Decimal(str(value)), ",f")
    except InvalidOperation:
        return str(value)


SETTING_GROUPS = [
    ("FLOW & RESPONSE", ["flow_unit", "response_time", "zero_cut", "integration_direction", "flow_direction"]),
    ("PIPE INSTALLATION", ["pipe_material", "advanced_pipe", "pipe_od", "pipe_thickness", "pipe_size", "piping_standard"]),
    ("GAS & CONVERSION", ["gas_type", "specific_heat_ratio", "molecular_weight", "span",
                          "conversion", "reference_temp", "pressure_input", "pipe_pressure"]),
    ("DISPLAY UNITS", ["additional_unit", "pressure_unit", "temperature_unit"]),
]
SELECTABLE = [BY_KEY[key] for _, keys in SETTING_GROUPS for key in keys]
SHORT_TITLES = {"pipe_od": "Pipe outer diameter", "pipe_thickness": "Pipe wall thickness",
                "reference_temp": "Reference temperature", "total_resolution": "Total resolution",
                "molecular_weight": "Molecular weight", "additional_unit": "Add. measurement unit"}


def input_spec(field, words, model=None, mode="resolution"):
    """One source of truth for the form's precision, units, examples and limits."""
    options = field.enum(model)
    if options is not None:
        return {"choices": options, "format": "choose a code"}
    if field.address == 196:
        return {"choices": None, "format": "select variant"}
    scale, unit = field.measure(words, mode)
    places = max(0, -scale.as_tuple().exponent)
    example = {
        101: Decimal("1"), 108: Decimal("60.30"), 109: Decimal("3.92"),
        111: Decimal("1.403"), 112: Decimal("28.97"), 113: Decimal("1.000"),
        116: Decimal("68.0") if words[209] == 1 else Decimal("20.0"),
        201: {0: Decimal("500.0"), 1: Decimal("5.000"), 2: Decimal("110.00")}.get(words[208], Decimal(0)),
    }.get(field.address, Decimal(field.minimum) * scale)
    return {"choices": None, "format": "0." + "0" * places if places else "whole number",
            "scale": scale, "unit": unit, "decimals": places,
            "minimum": decimal_text(Decimal(field.minimum) * scale),
            "maximum": decimal_text(Decimal(field.maximum) * scale),
            "example": format(example, ".{}f".format(places)),
            "raw_example": int(example / scale)}


def show_input_help(field, words, model, mode, ui):
    spec = input_spec(field, words, model, mode)
    rows = ["Current: {}".format(field.display(words, model, mode))]
    if spec.get("choices") is not None:
        rows += [("{}  {}{}".format(code, name,
                  "   < current" if code == field.raw_value(words) else ""), "accent")
                 for code, name in spec["choices"].items()]
        rows += ["Type a code or its label. Example: {}".format(next(iter(spec["choices"])))]
    else:
        rows += [("Enter the value in {}.".format(spec["unit"] or "the units shown below"), "accent"),
                 "Range: {} to {} {}".format(spec["minimum"], spec["maximum"], spec["unit"]),
                 "Decimals: up to {} | Smallest step: {} {}".format(
                     spec["decimals"], spec["scale"], spec["unit"]),
                 ("Example input: {}  (encodes as raw {})".format(spec["example"], spec["raw_example"]), "good"),
                 "Type the number only; the tool handles the decimal point and register conversion."]
    if field.note:
        rows.append((field.note, "dim"))
    rows += ["Enter = cancel / back.  ? = show this help again.",
             ("Advanced: raw:<integer> enters an unscaled register value.", "dim")]
    ui.box("EDIT  /  {}  [{}]".format(field.title, field.registers), rows)


def show_status(client, words, mode="resolution", out=print, ui=None, brief=False):
    ui = ui or Terminal(out=out)
    data = measurements(words, mode)
    ui.banner(client)
    ui.cards([
        ("FLOW", pretty_number(data["flow_display"]), data["flow_unit"], "warning" if words[1] & 0x100 else "accent"),
        ("PRESSURE", pretty_number(data["pressure"]), data["pressure_unit"], "accent"),
        ("STABILITY", "{} / 4".format(words[14]), data["stability"], "good" if words[14] == 4 else "warning")])
    ui.box("MEASUREMENTS", [
        "Total: {} {}  |  Leak: {} {}".format(pretty_number(data["total_display"]), data["total_unit"],
            flow_value(u32(words, 12), words[25], mode), data["flow_unit"]),
        "Peak: {}  |  Bottom: {} {}".format(flow_value(i32(words, 6), words[25], mode),
            flow_value(i32(words, 8), words[25], mode), data["flow_unit"]),
        "Basis: {}  /  {}  |  Pressure: {}".format(data["conversion"],
            BY_KEY["reference_temp"].display(words), label(BY_KEY["pressure_input"].choices, words[200])),
        "Initial settings: {}  |  Outputs: 1={}  2={}".format(
            label({0: "Incomplete", 1: "Complete"}, words[0]),
            "ON" if words[15] & 1 else "OFF", "ON" if words[15] & 2 else "OFF")])
    ui.box("METER STATUS  /  {}".format(data["error_mask"]),
           [(data["warnings"], "warning" if words[1] else "good")], "warning" if words[1] else "good")
    ui.message("Raw flow: {} | {} | Steps: {} / {}{}".format(
        data["flow_raw"], "resolution-scaled" if mode == "resolution" else "integer",
        label({k: str(v) for k, v in RESOLUTION.items()}, words[25]),
        label({k: str(v) for k, v in RESOLUTION.items()}, words[26]),
        " | " + data["timestamp_utc"][11:19] + " UTC" if brief else ""), "dim")
    if not brief or any(words[address] for address in (22, 23, 24)):
        ui.message("Operations: init={} / static leak={} / origin={}".format(
            label({0: "Idle", 1: "Executing", 2: "Finish"}, words[22]),
            label({0: "Idle", 1: "Executing", 2: "Success"}, words[23]),
            label({0: "Idle", 1: "Executing", 2: "Success", 3: "Failure"}, words[24])), "dim")
    if not brief:
        ui.message("Updated {} (UTC)".format(data["timestamp_utc"]), "dim")


def show_settings(words=None, model=None, out=print, ui=None, mode="resolution",
                  start=0, page_size=None, interactive=False):
    ui = ui or Terminal(out=out)
    end = min(len(SELECTABLE), start + (page_size or len(SELECTABLE)))
    lines = []
    # Fit a table to 60+ columns; use stacked entries on narrower terminals.
    compact = ui.width < 60
    if not compact:
        available = ui.width - 4 - 2 - 7 - 8
        format_width = 13 if ui.width >= 78 else 10
        title_width = min(24, (available - format_width) // 2 + 2)
        value_width = available - format_width - title_width
        widths = [2, 7, title_width, value_width, format_width]
        def row(cells):
            return "  ".join(ui.fit(cell, width) for cell, width in zip(cells, widths))
        lines.append((row(["#", "REG", "SETTING", "CURRENT", "INPUT FORMAT"]), "dim"))
    previous_group = None
    for index in range(start, end):
        field = SELECTABLE[index]
        group = next(name for name, keys in SETTING_GROUPS if field.key in keys)
        if group != previous_group:
            lines.append((group, "accent"))
            previous_group = group
        value = field.display(words, model, mode) if words is not None else "--"
        if words is not None and field.enum(model) is not None:
            value = label(field.enum(model), field.raw_value(words))
        try:
            form = input_spec(field, words, model, mode)["format"] if words is not None else (
                "choose a code" if field.choices else "see editor")
        except ToolError:
            form = "raw only"
        title = SHORT_TITLES.get(field.key, field.title)
        if compact:
            lines += ["{}  [{}] {}".format(index + 1, field.registers, title),
                      "   {} | input: {}".format(value, form)]
        else:
            lines.append(row([str(index + 1), field.registers, title, value, form]))
    ui.box("SETTINGS & EDIT  /  {}-{} OF {}".format(start + 1, end, len(SELECTABLE)), lines)
    if words is not None:
        ui.message("Read-only steps: flow [25]={}, total [26]={}. Size variant: {}.".format(
            label({k: str(v) for k, v in RESOLUTION.items()}, words[25]),
            label({k: str(v) for k, v in RESOLUTION.items()}, words[26]), model or "not selected"), "dim")
    if interactive:
        ui.message("Type a setting # (1-{}), register address, or name to EDIT.".format(len(SELECTABLE)), "accent")
        ui.message("[n] Next  [p] Previous  [r] Refresh  [m] Size variant  [Enter/b] Dashboard", "dim")
    else:
        ui.message("To edit: run the interactive tool and press S, then select a setting number.", "accent")
        ui.message("CLI: python3 fdg_tool.py --host <HOST> set <register-or-name> <value>", "dim")


CSV_COLUMNS = ["timestamp_utc", "flow_raw", "flow_display", "flow_mode", "flow_unit",
               "flow_resolution_code", "total_resolution_code", "conversion", "total_raw",
               "total_display", "total_unit", "pressure", "pressure_unit", "stability",
               "initialized", "error_mask", "warnings", "error"]


def sparkline(values, width, unicode=True):
    values = list(values)[-width:]
    if not values:
        return "Waiting for samples"
    low, high = min(values), max(values)
    bars = "▁▂▃▄▅▆▇█" if unicode else ".:-=+*#%@"
    if high == low:
        return bars[len(bars) // 2] * len(values)
    return "".join(bars[int((value - low) / (high - low) * (len(bars) - 1))]
                   for value in values)


def watch(client, interval=1.0, count=None, csv_path=None, mode="resolution", ui=None):
    ui = ui or Terminal()
    stream = None
    history = deque(maxlen=min(60, ui.width - 4))
    try:
        writer = None
        if csv_path:
            path = Path(csv_path)
            # A new file avoids silently mixing this schema with an existing log.
            stream = path.open("x", newline="", encoding="utf-8")
            writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
            writer.writeheader()
        if not ui.interactive:
            ui.line("Live flow ({}); Ctrl+C returns/stops.{}".format(
                "resolution-scaled" if mode == "resolution" else "integer",
                " DEMO - simulated values." if client.demo else ""))
        ui.cursor(False)
        sample = 0
        while count is None or sample < count:
            started = time.monotonic()
            try:
                words = read_ranges(client, STATUS_RANGES)
                data = measurements(words, mode)
                try:
                    history.append(Decimal(data["flow_display"]))
                except InvalidOperation:
                    pass
                if ui.interactive:
                    ui.clear()
                    show_status(client, words, mode, ui=ui, brief=True)
                    ui.box("FLOW TREND  /  LAST {} SAMPLES".format(len(history)), [
                        (sparkline(history, ui.width - 4, ui.unicode), "accent")])
                    ui.message("LIVE  /  Ctrl+C returns to the dashboard.{}".format(
                        "  CSV: " + str(csv_path) if csv_path else ""), "accent")
                else:
                    ui.line("{timestamp_utc}  Flow {flow_display} {flow_unit}  raw={flow_raw}  "
                            "P={pressure} {pressure_unit}  Stability={stability}  {warnings}".format(**data))
            except ToolError as exc:
                data = {"timestamp_utc": stamp(), "error": str(exc)}
                if ui.interactive:
                    ui.clear()
                    ui.banner(client, "CONNECTION ERROR")
                    ui.box("NO CURRENT READING", [(str(exc), "error"),
                           "Read-only polling will continue. Ctrl+C returns."], "error")
                else:
                    ui.line("{}  READ FAILED - no current reading: {}".format(data["timestamp_utc"], exc), "error")
            if writer:
                writer.writerow(data)
                stream.flush()
            sample += 1
            if count is None or sample < count:
                time.sleep(max(0, interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        ui.message("Live display stopped.", "dim")
    finally:
        ui.cursor(True)
        if stream:
            stream.close()


def choose_model(args, ui, ask=input):
    models = list(PIPE_SIZES)
    ui.box("SELECT THE METER'S SIZE VARIANT", [
        "Use the FD-G variant printed on the meter label. L versions use the same choices."] +
        ["{}  {}".format(index + 1, model) for index, model in enumerate(models)] +
        ["Type a number or model name. Enter cancels."])
    while True:
        value = ask("Size variant > ").strip()
        if not value:
            return False
        try:
            args.model = models[int(value) - 1] if value.isdigit() and 1 <= int(value) <= len(models) else model_name(value)
            return True
        except argparse.ArgumentTypeError as exc:
            ui.message(exc, "error")


def edit_form(client, journal, field, args, ui, ask=input):
    if not field.writable:
        ui.box("READ-ONLY VALUE", [field.title + " cannot be changed through this register.",
               "The meter reports this resolution; it is shown in the dashboard."], "warning")
        ask("Enter to return to settings: ")
        return
    if field.address == 196 and args.model is None:
        if not choose_model(args, ui, ask):
            return
    # Use this same unit/resolution snapshot for help, parsing, preview, and preflight.
    words = read_config(client)
    ui.clear()
    ui.banner(client, "EDIT SETTING")
    try:
        show_input_help(field, words, args.model, args.flow_mode, ui)
    except ToolError as exc:
        ui.box("RAW INPUT REQUIRED", [str(exc), "Use raw:<integer>, or Enter to go back."], "warning")
    while True:
        value = ask("New value > ").strip()
        if not value or value.lower() in ("b", "q"):
            return
        if value == "?":
            try:
                show_input_help(field, words, args.model, args.flow_mode, ui)
            except ToolError as exc:
                ui.message(exc, "warning")
            continue
        raw_mode = value.lower().startswith("raw:")
        if raw_mode:
            value = value[4:].strip()
        try:
            raw = field.parse(value, words, args.model, raw_mode, args.flow_mode)
            validate_change(field, raw, words)
        except ToolError as exc:
            ui.message("INPUT: " + str(exc), "error")
            continue
        try:
            changed = edit_setting(client, journal, field, value, args.model, raw_mode,
                                   confirm=ask, out=ui.message, before=words, ui=ui)
            if changed:
                ui.message("Setting saved and verified.", "good")
        except (ToolError, OSError) as exc:
            ui.box("CHANGE NOT COMPLETED", [(str(exc), "error")], "error")
        ask("Enter to return to settings: ")
        return


def settings_editor(client, journal, args, ui, ask=input):
    page = 0
    page_size = max(4, min(10, (ui.height - 12) // (2 if ui.width < 60 else 1)))
    pages = (len(SELECTABLE) + page_size - 1) // page_size
    while True:
        ui.clear()
        ui.banner(client, "SETTINGS & EDIT  /  PAGE {} OF {}".format(page + 1, pages))
        try:
            words = read_config(client)
            show_settings(words, args.model, ui=ui, mode=args.flow_mode,
                          start=page * page_size, page_size=page_size, interactive=True)
            choice = ask("Select setting > ").strip()
            if not choice or choice.lower() in ("b", "q"):
                return
            if choice.lower() in ("n", "p"):
                page = (page + (1 if choice.lower() == "n" else -1)) % pages
            elif choice.lower() == "m":
                choose_model(args, ui, ask)
            elif choice.lower() != "r":
                field = (SELECTABLE[int(choice) - 1] if choice.isdigit()
                         and 1 <= int(choice) <= len(SELECTABLE) else find_field(choice))
                edit_form(client, journal, field, args, ui, ask)
        except (ToolError, OSError, argparse.ArgumentTypeError) as exc:
            ui.box("SETTINGS ERROR", [(str(exc), "error")], "error")
            if ask("Enter to retry, or b to return to dashboard: ").strip().lower() == "b":
                return


def menu(client, journal, args, ui=None):
    ui = ui or Terminal()
    while True:
        try:
            ui.clear()
            try:
                show_status(client, read_ranges(client, STATUS_RANGES), args.flow_mode, ui=ui, brief=True)
            except ToolError as exc:
                ui.banner(client, "CONNECTION ERROR")
                ui.box("NO CURRENT READING", [(str(exc), "error")], "error")
            ui.box("ACTIONS", [
                ("[S] Settings & edit  [W] Live  [B] Snapshot  [M] Size  [Enter] Refresh  [Q] Quit", "accent")])
            choice = input("Dashboard > ").strip().lower()
            if choice == "q":
                return
            if choice == "w":
                watch(client, args.interval, mode=args.flow_mode, ui=ui)
            elif choice in ("s", "e"):
                settings_editor(client, journal, args, ui)
            elif choice == "b":
                ui.message("Saved " + str(journal.snapshot(read_config(client), args.model)), "good")
                input("Enter to return: ")
            elif choice == "m":
                choose_model(args, ui)
            elif choice not in ("", "r"):
                ui.message("Choose one of the listed commands.", "warning")
                input("Enter to return: ")
        except (ToolError, OSError, argparse.ArgumentTypeError) as exc:
            ui.message("ERROR: {}".format(exc), "error")
            input("Enter to return: ")
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            return


def positive_interval(value):
    number = float(value)
    if not 0.1 <= number <= 3600:
        raise argparse.ArgumentTypeError("Interval must be 0.1 to 3600 seconds.")
    return number


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit-id", type=int, default=1)
    parser.add_argument("--modpoll", default="modpoll", help="Modpoll executable path")
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--model", type=model_name, help="FD-G25/50/80/125/200 (also L versions)")
    parser.add_argument("--log-dir", default="fdg_logs", help="Settings snapshots and write audit log")
    parser.add_argument("--demo", action="store_true", help="Use simulated registers; no network")
    parser.add_argument("--flow-mode", choices=["resolution", "integer"], default="resolution",
                        help="resolution = raw x register 25/26; integer = unscaled (see README)")
    parser.add_argument("--interval", type=positive_interval, default=1.0, help="Live polling interval in seconds")
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                        help="Terminal colors (auto respects NO_COLOR)")
    parser.add_argument("--ascii", action="store_true", help="Use ASCII borders instead of Unicode")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("menu", help="Interactive SSH terminal menu (default)")
    sub.add_parser("status", help="Read flow and statuses once")
    sub.add_parser("settings", help="Read supported settings")
    sub.add_parser("catalog", help="List setting descriptions without connecting")
    sub.add_parser("snapshot", help="Save a settings snapshot")
    live = sub.add_parser("watch", help="Live readings; Ctrl+C to stop")
    live.add_argument("--count", type=int, help="Stop after this many samples")
    live.add_argument("--csv", help="Write readings to a new CSV file")
    change = sub.add_parser("set", help="Preview, confirm, write and verify one setting")
    change.add_argument("field", help="Setting name or zero-based manual address")
    change.add_argument("value", help="Code/label or value in the displayed engineering unit")
    change.add_argument("--raw", action="store_true", help="Interpret numeric input as raw integer")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or not 0 <= args.unit_id <= 255 or not 0.01 <= args.timeout <= 10:
        parser.error("Port must be 1-65535; unit ID 0-255; timeout 0.01-10 seconds.")
    if args.command == "watch" and args.count is not None and args.count < 1:
        parser.error("--count must be at least 1.")
    try:
        ui = Terminal(color=args.color, ascii_only=args.ascii)
        if args.command == "catalog":
            show_settings(model=args.model, ui=ui)
            for field in FIELDS:
                options = field.enum(args.model)
                if options:
                    print("{}: {}".format(field.key, "; ".join("{}={}".format(k, v) for k, v in options.items())))
                if field.note:
                    print("{}: {}".format(field.key, field.note))
            return 0
        client = Demo() if args.demo else Modpoll(args.host, args.port, args.unit_id, args.modpoll, args.timeout)
        journal = Journal(args.log_dir, client, args.flow_mode)
        command = args.command or "menu"
        if command == "menu":
            menu(client, journal, args, ui)
        elif command == "status":
            show_status(client, read_ranges(client, STATUS_RANGES), args.flow_mode, ui=ui)
        elif command == "settings":
            ui.banner(client, "SETTINGS")
            show_settings(read_config(client), args.model, ui=ui, mode=args.flow_mode)
        elif command == "watch":
            watch(client, args.interval, args.count, args.csv, args.flow_mode, ui)
        elif command == "snapshot":
            print(journal.snapshot(read_config(client), args.model))
        elif command == "set":
            field = find_field(args.field)
            if not field.writable:
                raise ToolError("Registers {} are read-only; they are displayed only.".format(field.registers))
            words = read_config(client)
            ui.banner(client, "EDIT SETTING")
            if not args.raw:
                # The form and CLI show the same unit/precision instructions.
                try:
                    show_input_help(field, words, args.model, args.flow_mode, ui)
                except ToolError:
                    pass  # parse() supplies the actionable validation error below.
            edit_setting(client, journal, field, args.value, args.model, args.raw,
                         before=words, ui=ui, out=ui.message)
        return 0
    except (ToolError, OSError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
