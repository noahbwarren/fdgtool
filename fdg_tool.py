#!/usr/bin/env python3
"""Keyence FD-G terminal configuration tool. Python 3.9+, standard library only.

Addresses shown to the operator are the manual's ZERO-BASED relative addresses.
Only Modpoll sees address + 1, with -0 deliberately omitted for Modpoll 3.4.
"""

import argparse
import csv
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional


VERSION = "1.0.0"
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

    def measure(self, words):
        if self.address == 201:
            if words[208] not in PRESSURE:
                raise ToolError("Unknown pressure unit; use --raw or correct register 208.")
            return PRESSURE[words[208]]
        if self.address == 116:
            return Decimal("0.1"), label({0: "degC", 1: "degF"}, words[209])
        return Decimal(self.scale), self.unit

    def display(self, words, model=None):
        raw = self.raw_value(words)
        options = self.enum(model)
        if options is not None:
            return "{} (code {})".format(label(options, raw), raw)
        if self.address == 196:
            return "code {} (size variant not selected)".format(raw)
        try:
            scale, unit = self.measure(words)
        except ToolError:
            return "{} raw (unknown unit)".format(raw)
        return "{} {}".format(decimal_text(Decimal(raw) * scale), unit).strip()

    def parse(self, text, words, model=None, raw_mode=False):
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
        scale = Decimal(1) if raw_mode or options else self.measure(words)[0]
        raw = number / scale
        if raw != raw.to_integral_value():
            raise ToolError("Value must be an exact multiple of {}.".format(scale))
        raw = int(raw)
        if options is not None and raw not in options:
            raise ToolError("Allowed codes: " + ", ".join(map(str, options)))
        if not self.minimum <= raw <= self.maximum:
            raise ToolError("Raw value must be {} to {}.".format(self.minimum, self.maximum))
        return raw


FIELDS = [
    Field("flow_resolution", 25, "Flow resolution", 0, 3,
          choices={0: "0.001", 1: "0.01", 2: "0.1", 3: "1"}, writable=False),
    Field("total_resolution", 26, "Integrated flow resolution", 0, 3,
          choices={0: "0.001", 1: "0.01", 2: "0.1", 3: "1"}, writable=False),
    Field("response_time", 100, "Response time", 0, 7,
          choices=dict(enumerate(["1s", "2.5s", "5s", "10s", "30s", "60s", "120s", "200s"]))),
    Field("zero_cut", 101, "Zero cut flow rate", 0, 999999, width=2, unit="raw counts",
          note="Enter the combined raw integer for registers 101-102; no decimal scaling is assumed."),
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
    if value in BY_KEY:
        return BY_KEY[value]
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
    def __init__(self, directory, client):
        self.directory = Path(directory)
        self.client = client

    def metadata(self):
        return {"timestamp_utc": stamp(), "host": self.client.host,
                "port": self.client.port, "unit_id": self.client.unit_id,
                "demo": self.client.demo, "tool_version": VERSION,
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
                                          "display": f.display(words, model),
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
                 confirm=input, out=print):
    # Reject read-only/unknown words before connecting to the device.
    if not field.writable:
        raise ToolError("Registers {} are read-only; they are displayed only.".format(field.registers))
    before = read_config(client)
    raw = field.parse(value, before, model, raw_mode)
    proposed = validate_change(field, raw, before)
    old = field.raw_value(before)
    out("{} [{}] {}: {} -> {}".format(client.host, field.registers, field.title,
                                     field.display(before, model), field.display(proposed, model)))
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
                    other.display(before, model), other.display(after, model)))
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


def show_status(client, words, mode="resolution", out=print):
    data = measurements(words, mode)
    out("\n{} | {}:{} | unit ID {}{}".format(data["timestamp_utc"], client.host, client.port,
        client.unit_id, " | DEMO - simulated registers" if client.demo else ""))
    mode_label = "resolution-scaled" if mode == "resolution" else "integer"
    out("Flow ({}): {} {} | raw signed: {}".format(mode_label, data["flow_display"],
                                                              data["flow_unit"], data["flow_raw"]))
    out("Peak: {} | Bottom: {} | Leak: {} {}".format(
        flow_value(i32(words, 6), words[25], mode), flow_value(i32(words, 8), words[25], mode),
        flow_value(u32(words, 12), words[25], mode), data["flow_unit"]))
    out("Integrated: {} {} | raw: {}".format(data["total_display"], data["total_unit"], data["total_raw"]))
    out("Pressure: {} {} | Input: {} | Conversion: {} | Ref: {}".format(
        data["pressure"], data["pressure_unit"], BY_KEY["pressure_input"].display(words),
        data["conversion"], BY_KEY["reference_temp"].display(words)))
    out("Stability: {} | Initial settings: {} | Output 1: {} | Output 2: {}".format(
        data["stability"], label({0: "Incomplete", 1: "Complete"}, words[0]),
        "ON" if words[15] & 1 else "OFF", "ON" if words[15] & 2 else "OFF"))
    out("Errors/warnings {}: {}".format(data["error_mask"], data["warnings"]))
    out("Resolution [25]: {} | Integrated [26]: {} (read-only)".format(
        label({k: str(v) for k, v in RESOLUTION.items()}, words[25]),
        label({k: str(v) for k, v in RESOLUTION.items()}, words[26])))
    out("Operations: initialization={} | static leakage={} | origin adjustment={}".format(
        label({0: "Idle", 1: "Executing", 2: "Finish"}, words[22]),
        label({0: "Idle", 1: "Executing", 2: "Success"}, words[23]),
        label({0: "Idle", 1: "Executing", 2: "Success", 3: "Failure"}, words[24])))


def show_settings(words=None, model=None, out=print):
    out("\nAddresses are zero-based as printed in the manual. Pipe size variant: " + (model or "not selected"))
    for field in FIELDS:
        access = "RW" if field.writable else "RO"
        value = field.display(words, model) if words is not None else ""
        out("{:>7} {:2} {:23} {}".format(field.registers, access, field.key, value or field.title))


CSV_COLUMNS = ["timestamp_utc", "flow_raw", "flow_display", "flow_mode", "flow_unit",
               "flow_resolution_code", "total_resolution_code", "conversion", "total_raw",
               "total_display", "total_unit", "pressure", "pressure_unit", "stability",
               "initialized", "error_mask", "warnings", "error"]


def watch(client, interval=1.0, count=None, csv_path=None, mode="resolution"):
    stream = None
    try:
        writer = None
        if csv_path:
            path = Path(csv_path)
            # A new file avoids silently mixing this schema with an existing log.
            stream = path.open("x", newline="", encoding="utf-8")
            writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
            writer.writeheader()
        print("Live flow ({}); Ctrl+C returns/stops.{}".format(
            "resolution-scaled" if mode == "resolution" else "integer",
            " DEMO - simulated values." if client.demo else ""))
        sample = 0
        while count is None or sample < count:
            started = time.monotonic()
            try:
                data = measurements(read_ranges(client, STATUS_RANGES), mode)
                print("{timestamp_utc}  Flow {flow_display} {flow_unit}  raw={flow_raw}  "
                      "P={pressure} {pressure_unit}  Stability={stability}  {warnings}".format(**data))
            except ToolError as exc:
                data = {"timestamp_utc": stamp(), "error": str(exc)}
                print("{}  READ FAILED - no current reading: {}".format(data["timestamp_utc"], exc))
            if writer:
                writer.writerow(data)
                stream.flush()
            sample += 1
            if count is None or sample < count:
                time.sleep(max(0, interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("\nLive display stopped.")
    finally:
        if stream:
            stream.close()


def menu(client, journal, args):
    print("FD-G terminal tool {} | all register addresses are manual addresses".format(VERSION))
    print("Flow display: {}. Raw integers are also shown; see README for decimal interpretation.".format(args.flow_mode))
    while True:
        try:
            try:
                show_status(client, read_ranges(client, STATUS_RANGES), args.flow_mode)
            except ToolError as exc:
                print("\nREAD FAILED - no current reading: {}".format(exc))
            print("\n[w] Live readings  [s] Settings  [e] Edit  [b] Save snapshot")
            print("[m] Select size variant  [r/Enter] Refresh  [q] Quit")
            choice = input("> ").strip().lower()
            if choice == "q":
                return
            if choice == "w":
                watch(client, args.interval, mode=args.flow_mode)
            elif choice == "s":
                show_settings(read_config(client), args.model)
                input("Enter to return: ")
            elif choice == "b":
                print("Saved " + str(journal.snapshot(read_config(client), args.model)))
            elif choice == "m":
                print("Available variants (also covers L versions): " + ", ".join(PIPE_SIZES))
                value = input("Variant, or Enter to cancel: ").strip()
                if value:
                    args.model = model_name(value)
            elif choice == "e":
                words = read_config(client)
                show_settings(words, args.model)
                key = input("Setting name or manual address (Enter cancels): ").strip()
                if not key:
                    continue
                field = find_field(key)
                if not field.writable:
                    raise ToolError("{} is read-only.".format(field.title))
                print("{}: {}".format(field.title, field.display(words, args.model)))
                options = field.enum(args.model)
                if options:
                    print("Choices: " + "; ".join("{}={}".format(k, v) for k, v in options.items()))
                if field.note:
                    print(field.note)
                if options is None and field.address != 196:
                    scale, unit = field.measure(words)
                    print("Enter {} to {} {}; step {}. Prefix raw: to enter a register integer.".format(
                        Decimal(field.minimum) * scale, Decimal(field.maximum) * scale, unit, scale))
                value = input("New value (Enter cancels): ").strip()
                if value:
                    raw_mode = value.lower().startswith("raw:")
                    if raw_mode:
                        value = value[4:].strip()
                    edit_setting(client, journal, field, value, args.model, raw_mode)
            elif choice not in ("", "r"):
                print("Choose one of the listed commands.")
        except (ToolError, OSError, argparse.ArgumentTypeError) as exc:
            print("ERROR: {}".format(exc))
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
    change.add_argument("value", help="Code/label or engineering value; zero_cut uses raw counts")
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
        if args.command == "catalog":
            show_settings(model=args.model)
            for field in FIELDS:
                options = field.enum(args.model)
                if options:
                    print("{}: {}".format(field.key, "; ".join("{}={}".format(k, v) for k, v in options.items())))
                if field.note:
                    print("{}: {}".format(field.key, field.note))
            return 0
        client = Demo() if args.demo else Modpoll(args.host, args.port, args.unit_id, args.modpoll, args.timeout)
        journal = Journal(args.log_dir, client)
        command = args.command or "menu"
        if command == "menu":
            menu(client, journal, args)
        elif command == "status":
            show_status(client, read_ranges(client, STATUS_RANGES), args.flow_mode)
        elif command == "settings":
            show_settings(read_config(client), args.model)
        elif command == "watch":
            watch(client, args.interval, args.count, args.csv, args.flow_mode)
        elif command == "snapshot":
            print(journal.snapshot(read_config(client), args.model))
        elif command == "set":
            edit_setting(client, journal, find_field(args.field), args.value, args.model, args.raw)
        return 0
    except (ToolError, OSError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
