"""Offline behavior tests. No physical meter or Modpoll installation is used."""

import ast
import csv
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import fdg_tool as fdg


class FakeModpoll:
    """Emulates Modpoll 3.4 output and a device behind its subprocess interface."""

    def __init__(self):
        self.words = dict(fdg.Demo().words)
        self.calls = []
        self.writes = []
        self.output_override = None
        self.write_error = False
        self.ignore_write = False
        self.fail_after_write = False
        self.auto_change_resolution = False

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        address = int(args[args.index("-r") + 1]) - 1
        host_index = args.index(fdg.DEFAULT_HOST)
        values = [int(x) for x in args[host_index + 1:]]
        if values:
            self.writes.append((address, values))
            if not self.ignore_write:
                self.words.update({address + index: value for index, value in enumerate(values)})
            if self.auto_change_resolution and address == 199:
                self.words[25] = 2
            if self.write_error:
                raise subprocess.TimeoutExpired(args, 2)
            return subprocess.CompletedProcess(args, 0, "Written {} registers\n".format(len(values)), "")
        if self.writes and self.fail_after_write:
            return subprocess.CompletedProcess(args, 1, "", "Reply time-out!")
        if self.output_override is not None:
            return subprocess.CompletedProcess(args, 0, self.output_override, "")
        count = int(args[args.index("-c") + 1])
        rows = ["[{}]: 0x{:04X}".format(i + 1, self.words[i])
                for i in range(address, address + count)]
        output = ("modpoll 3.4 - FieldTalk(tm) Modbus(R) Master Simulator\n"
                  "Slave configuration...: address = 1, start reference = {}\n"
                  "TRACELOG: Read multiple registers 1 {}\n"
                  "TRACELOG: Recv(13): 00 01 00 00 00 07 01 03 04 00 01 01 00\n"
                  "-- Polling slave...\n{}\nTRACELOG: Close connection\n").format(
                      address + 1, address + 1, "\n".join(rows))
        return subprocess.CompletedProcess(args, 0, output, "")


class ToolTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parent / "tmp" / "tests"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        # TemporaryDirectory's recursive cleanup must stay inside this workspace.
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to(test_root.resolve()))
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "logs"
        self.fake = FakeModpoll()
        self.client = fdg.Modpoll(fdg.DEFAULT_HOST, runner=self.fake)
        self.journal = fdg.Journal(self.directory, self.client)
        self.messages = []

    def edit(self, name, value, **kwargs):
        return fdg.edit_setting(self.client, self.journal, fdg.find_field(name), value,
                                confirm=lambda _: "APPLY", out=self.messages.append, **kwargs)

    def events(self):
        return [json.loads(line) for line in (self.directory / "changes.jsonl").read_text().splitlines()]

    def test_pi_status_trace_and_zero_address_translation(self):
        self.assertEqual(self.client.read(0, 2), {0: 1, 1: 0x100})
        command = self.fake.calls[0]
        self.assertNotIn("-0", command)
        self.assertEqual(command[command.index("-r") + 1], "1")
        self.assertEqual(fdg.warning_names(0x100), "Reverse flow")

    def test_pi_flow_trace_signed_and_two_display_interpretations(self):
        words = self.client.read(4, 2)
        self.assertEqual(fdg.i32(words, 4), -18524)
        self.assertEqual(fdg.flow_value(fdg.i32(words, 4), 1, "resolution"), "-185.24")
        self.assertEqual(fdg.flow_value(fdg.i32(words, 4), 1, "integer"), "-18524")

    def test_parser_rejects_missing_duplicate_wrong_and_invalid_rows(self):
        for text in ["[1]: 1", "[1]: 1\n[1]: 2\n[2]: 3", "[0]: 1\n[1]: 0",
                     "[1]: 0x10000\n[2]: 1", "[1]: -40000\n[2]: 0"]:
            with self.subTest(text=text):
                self.fake.output_override = text
                with self.assertRaises(fdg.ToolError):
                    self.client.read(0, 2)

    def test_parser_accepts_signed_decimal_words(self):
        self.fake.output_override = "[5]: -18524\n[6]: -1\n"
        self.assertEqual(fdg.i32(self.client.read(4, 2), 4), -18524)

    def test_zero_cut_pair_is_one_write_low_word_first(self):
        self.assertTrue(self.edit("101", "123456"))
        self.assertEqual(self.fake.writes, [(101, [57920, 1])])
        write_command = [args for args in self.fake.calls if args[-1] == "1" and "57920" in args][0]
        self.assertEqual(write_command[write_command.index("-r") + 1], "102")
        self.assertNotIn("-c", write_command)
        snapshots = list(self.directory.glob("*-before.json"))
        self.assertEqual(len(snapshots), 1)
        before = json.loads(snapshots[0].read_text())
        self.assertEqual(before["settings"]["zero_cut"]["raw"], 180)
        self.assertEqual([event["event"] for event in self.events()], ["write_attempt", "write_verified"])

    def test_read_only_resolution_rejected_before_device_read(self):
        for name in ("25", "26"):
            with self.assertRaisesRegex(fdg.ToolError, "read-only"):
                self.edit(name, "0")
        self.assertEqual(self.fake.calls, [])

    def test_upper_word_and_unapproved_addresses_are_not_writable(self):
        for address in ("102", "220", "202", "204", "103"):
            with self.assertRaises(fdg.ToolError):
                fdg.find_field(address)

    def test_exact_requested_editable_register_coverage(self):
        actual = {address for field in fdg.FIELDS if field.writable
                  for address in range(field.address, field.address + field.width)}
        expected = (set(range(100, 103)) | set(range(106, 114)) | {116}
                    | set(range(194, 202)) | set(range(207, 210)))
        self.assertEqual(actual, expected)

    def test_engineering_units_fixed_precision_and_raw_mode(self):
        words = self.fake.words
        self.assertEqual(fdg.BY_KEY["pipe_od"].parse("60.30", words), 6030)
        self.assertEqual(fdg.BY_KEY["specific_heat_ratio"].parse("1.402", words), 1402)
        self.assertEqual(fdg.BY_KEY["reference_temp"].parse("20.0", words), 200)
        words[208] = 2
        self.assertEqual(fdg.BY_KEY["pipe_pressure"].parse("58.23", words), 5823)
        self.assertEqual(fdg.BY_KEY["pipe_od"].parse("6030", words, raw_mode=True), 6030)
        for text in ("60.301", "NaN", "Infinity", "-1", "301"):
            with self.subTest(text=text), self.assertRaises(fdg.ToolError):
                fdg.BY_KEY["pipe_od"].parse(text, words)

    def test_enums_labels_bounds_and_inverted_additional_unit(self):
        self.assertEqual(fdg.BY_KEY["flow_unit"].parse("CFM", self.fake.words), 2)
        self.assertEqual(fdg.BY_KEY["additional_unit"].parse("ON", self.fake.words), 0)
        self.assertEqual(fdg.BY_KEY["additional_unit"].parse("OFF", self.fake.words), 1)
        with self.assertRaises(fdg.ToolError):
            self.edit("response_time", "8")
        self.assertEqual(self.fake.writes, [])

    def test_pipe_size_requires_and_validates_specific_variant(self):
        field = fdg.BY_KEY["pipe_size"]
        with self.assertRaises(fdg.ToolError):
            field.parse("1", self.fake.words)
        with self.assertRaises(fdg.ToolError):
            field.parse("3", self.fake.words, "FD-G25")
        self.assertEqual(field.parse("3", self.fake.words, "FD-G50"), 3)
        self.assertEqual(fdg.model_name("FD-G50(L)"), "FD-G50")
        self.assertEqual(fdg.model_name("FD-G50L"), "FD-G50")

    def test_pipe_geometry_and_inactive_advanced_settings(self):
        with self.assertRaisesRegex(fdg.ToolError, "twice"):
            self.edit("pipe_od", "1.00")
        self.fake.words[107] = 0
        with self.assertRaisesRegex(fdg.ToolError, "ON before"):
            self.edit("pipe_od", "60.30")
        self.assertFalse(self.fake.writes)

    def test_cancel_and_already_set_never_write_or_create_logs(self):
        result = fdg.edit_setting(self.client, self.journal, fdg.BY_KEY["flow_unit"], "CFM",
                                  confirm=lambda _: "", out=self.messages.append)
        self.assertFalse(result)
        self.assertFalse(self.edit("flow_unit", "m3/h"))
        self.assertFalse(self.fake.writes)
        self.assertFalse(self.directory.exists())

    def test_configuration_change_during_preview_aborts(self):
        def confirm(_):
            self.fake.words[208] = 2
            return "APPLY"
        with self.assertRaisesRegex(fdg.ToolError, "changed since"):
            fdg.edit_setting(self.client, self.journal, fdg.BY_KEY["pipe_pressure"], "500.1",
                             confirm=confirm, out=self.messages.append)
        self.assertFalse(self.fake.writes)

    def test_snapshot_must_succeed_before_any_write(self):
        with patch.object(self.journal, "snapshot", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.edit("flow_unit", "CFM")
        self.assertFalse(self.fake.writes)

    def test_mismatch_is_reported_without_retry_or_rollback(self):
        self.fake.ignore_write = True
        with self.assertRaisesRegex(fdg.ToolError, "Read-back mismatch"):
            self.edit("flow_unit", "CFM")
        self.assertEqual(self.fake.writes, [(199, [2])])
        self.assertEqual(self.events()[-1]["event"], "write_not_verified")

    def test_lost_write_response_but_successful_readback_is_recorded(self):
        self.fake.write_error = True
        self.assertTrue(self.edit("flow_unit", "CFM"))
        self.assertEqual(len(self.fake.writes), 1)
        self.assertEqual(self.events()[-1]["event"], "write_verified")
        self.assertIn("timed out", self.events()[-1]["write_error"])

    def test_unreadable_write_result_is_unknown_without_retry(self):
        self.fake.fail_after_write = True
        with self.assertRaisesRegex(fdg.ToolError, "outcome UNKNOWN"):
            self.edit("flow_unit", "CFM")
        self.assertEqual(len(self.fake.writes), 1)
        self.assertEqual(self.events()[-1]["event"], "write_outcome_unknown")

    def test_firmware_side_effects_are_reported_and_snapshotted(self):
        self.fake.auto_change_resolution = True
        self.edit("flow_unit", "CFM")
        self.assertTrue(any("Also changed [25]" in text for text in self.messages))
        after = json.loads(next(self.directory.glob("*-after.json")).read_text())
        self.assertEqual(after["registers"]["199"], 2)
        self.assertEqual(after["registers"]["25"], 2)

    def test_unknown_status_codes_keep_raw_data_visible(self):
        words = self.fake.words
        words[25], words[208] = 99, 99
        data = fdg.measurements(words, "resolution")
        self.assertEqual(data["flow_raw"], -18524)
        self.assertIn("unavailable", data["flow_display"])
        self.assertIn("unknown unit", data["pressure_unit"])

    def test_live_csv_contains_decoded_values_and_refuses_overwrite(self):
        path = Path(self.temp.name) / "flow.csv"
        with redirect_stdout(io.StringIO()):
            fdg.watch(self.client, count=1, csv_path=path)
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["flow_raw"], "-18524")
        self.assertEqual(rows[0]["flow_display"], "-185.24")
        self.assertEqual(rows[0]["warnings"], "Reverse flow")
        with self.assertRaises(FileExistsError):
            fdg.watch(self.client, count=1, csv_path=path)

    def test_failed_read_csv_has_no_stale_flow_values(self):
        path = Path(self.temp.name) / "failed.csv"
        self.fake.output_override = "Reply time-out!"
        with redirect_stdout(io.StringIO()):
            fdg.watch(self.client, count=1, csv_path=path)
        with path.open(newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["flow_display"], "")
        self.assertTrue(row["error"])

    def test_python_39_grammar(self):
        source = Path(fdg.__file__).read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
