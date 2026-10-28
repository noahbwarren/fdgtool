"""Offline behavior tests. No physical meter or Modpoll installation is used."""

import ast
import csv
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import fdg_tool as fdg


class FakeModpoll:
    """Emulates Modpoll 3.4 output and a device behind its subprocess interface."""

    def __init__(self):
        self.words = dict(fdg.Demo().words)
        self.calls = []
        self.run_options = []
        self.writes = []
        self.output_override = None
        self.write_error = False
        self.ignore_write = False
        self.fail_after_write = False
        self.auto_change_resolution = False

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        self.run_options.append(kwargs)
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
        self.assertEqual(self.fake.run_options[0]["stdin"], subprocess.DEVNULL)
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
        self.assertTrue(self.edit("101", "123456", raw_mode=True))
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

    def ui(self, width=80, height=30):
        return fdg.Terminal(color="never", ascii_only=True, width=width, height=height,
                            interactive=False, out=self.messages.append)

    def editor(self, answers, width=80):
        replies = iter(answers)
        prompts = []
        def ask(prompt):
            prompts.append(prompt)
            return next(replies)
        args = SimpleNamespace(model=None, flow_mode="resolution")
        fdg.settings_editor(self.client, self.journal, args, self.ui(width), ask)
        return args, prompts

    def test_settings_screen_edits_pressure_directly_with_decimal_help(self):
        self.fake.words.update({208: 2, 201: 11000})
        _, prompts = self.editor(["19", "110.25", "APPLY", "", ""])
        self.assertEqual(self.fake.writes, [(201, [11025])])
        text = "\n".join(self.messages)
        self.assertIn("Type a setting #", text)
        self.assertIn("Enter the value in psi", text)
        self.assertIn("Decimals: up to 2", text)
        self.assertIn("Example input: 110.00", text)
        self.assertIn("110.25 psi", text)
        self.assertIn("REVIEW CHANGE", text)
        self.assertIn("Select setting > ", prompts)

    def test_input_error_stays_in_form_and_accepts_corrected_value(self):
        self.editor(["8", "60.301", "60.31", "APPLY", "", ""])
        self.assertEqual(self.fake.writes, [(108, [6031])])
        self.assertTrue(any("up to 2 decimal" in line for line in self.messages))

    def test_enum_selection_in_settings_needs_no_separate_edit_menu(self):
        self.editor(["1", "2", "APPLY", "", ""])
        self.assertEqual(self.fake.writes, [(199, [2])])
        self.assertTrue(any("CFM" in line for line in self.messages))

    def test_settings_accepts_displayed_names_and_addresses(self):
        self.assertEqual(fdg.find_field("Flow unit").address, 199)
        self.assertEqual(fdg.find_field("Pipe wall thickness").address, 109)
        self.assertEqual(fdg.find_field("Reference temperature").address, 116)
        self.editor(["Flow unit", "CFM", "APPLY", "", ""])
        self.assertEqual(self.fake.writes, [(199, [2])])

    def test_readonly_value_and_cancelled_form_do_not_write(self):
        self.editor(["25", "", "8", "", ""])
        self.assertFalse(self.fake.writes)
        self.assertFalse(self.directory.exists())

    def test_single_settings_list_and_refresh_are_read_only(self):
        self.editor(["r", ""])
        self.assertFalse(self.fake.writes)
        text = "\n".join(self.messages)
        self.assertIn("ALL SETTINGS", text)
        self.assertNotIn("PAGE ", text)
        self.assertNotIn("choose a code", text)
        self.assertIn("199", text)
        self.assertIn("209", text)
        self.assertIn("0=m3/h, 1=L/min, 2=CFM", text)
        self.assertIn("0=kPa, 1=bar, 2=psi", text)
        self.assertIn("0=C, 1=F", text)
        self.assertIn("[Enter/b] Dashboard", text)

    def test_pipe_size_edit_opens_variant_selector_when_needed(self):
        args, _ = self.editor(["10", "2", "2", "APPLY", "", ""])
        self.assertEqual(args.model, "FD-G50")
        self.assertEqual(self.fake.writes, [(196, [2])])

    def test_precision_examples_follow_units_and_dynamic_resolution(self):
        words = self.fake.words
        for address, decimals, example, encoded in [
                (108, 2, "60.30", 6030), (109, 2, "3.92", 392),
                (111, 3, "1.403", 1403), (112, 2, "28.97", 2897),
                (113, 3, "1.000", 1000), (116, 1, "20.0", 200)]:
            with self.subTest(address=address):
                spec = fdg.input_spec(fdg.BY_ADDRESS[address], words)
                self.assertEqual((spec["decimals"], spec["example"], spec["raw_example"]),
                                 (decimals, example, encoded))
        words[208], words[209] = 2, 1
        spec = fdg.input_spec(fdg.BY_ADDRESS[201], words)
        self.assertEqual((spec["unit"], spec["example"], spec["maximum"]), ("psi", "110.00", "600.00"))
        self.assertEqual(fdg.input_spec(fdg.BY_ADDRESS[116], words)["example"], "68.0")

    def test_zero_cut_engineering_input_and_explicit_raw_input(self):
        self.fake.words[25] = 2  # Current meter resolution 0.1
        self.assertTrue(self.edit("zero_cut", "2.5"))
        self.assertEqual(self.fake.writes, [(101, [25, 0])])
        with self.assertRaises(fdg.ToolError):
            fdg.BY_KEY["zero_cut"].parse("2.55", self.fake.words)
        self.assertEqual(fdg.BY_KEY["zero_cut"].parse("2.5", self.fake.words, flow_mode="resolution"), 25)
        self.assertEqual(fdg.BY_KEY["zero_cut"].parse("25", self.fake.words, flow_mode="integer"), 25)
        self.assertEqual(fdg.BY_KEY["zero_cut"].parse("25", self.fake.words, raw_mode=True), 25)

    def test_unit_change_while_form_is_open_is_rejected(self):
        self.fake.words.update({208: 2, 201: 11000})
        replies = iter(["111.00", "APPLY", ""])
        def ask(prompt):
            answer = next(replies)
            if prompt.startswith("New value"):
                self.fake.words[208] = 0
            return answer
        fdg.edit_form(self.client, self.journal, fdg.BY_KEY["pipe_pressure"],
                      SimpleNamespace(model=None, flow_mode="resolution"), self.ui(), ask)
        self.assertFalse(self.fake.writes)
        self.assertTrue(any("changed since" in text for text in self.messages))

    def test_layout_fits_narrow_and_standard_ssh_terminals_without_ansi(self):
        for width in (40, 60, 80, 120):
            with self.subTest(width=width):
                self.messages.clear()
                ui = self.ui(width)
                fdg.show_status(self.client, self.fake.words, ui=ui)
                fdg.show_settings(self.fake.words, ui=ui, interactive=True)
                fdg.show_input_help(fdg.BY_KEY["pipe_od"], self.fake.words, None, "resolution", ui)
                self.assertTrue(all(len(line) <= width for line in self.messages),
                                [(len(line), line) for line in self.messages if len(line) > width])
                self.assertFalse(any("\033[" in line for line in self.messages))

    def test_color_auto_respects_no_color_and_explicit_setting(self):
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            self.assertFalse(fdg.Terminal(color="auto", interactive=True, plain=False).color)
            self.assertTrue(fdg.Terminal(color="always", interactive=False, plain=False).color)
        self.assertFalse(fdg.Terminal(color="never", interactive=True, plain=False).color)

    def test_live_terminal_restores_cursor_after_failed_read(self):
        self.fake.output_override = "Reply time-out!"
        ui = fdg.Terminal(color="never", ascii_only=True, interactive=True,
                          width=80, out=self.messages.append, plain=False)
        fdg.watch(self.client, count=1, ui=ui)
        self.assertIn("\033[?25l", self.messages)
        self.assertEqual(self.messages[-1], "\033[?25h")
        self.assertTrue(any("NO CURRENT READING" in line for line in self.messages))
        self.assertTrue(any("errors/warnings are unavailable" in line for line in self.messages))
        self.assertFalse(any("No errors" in line for line in self.messages))
        self.assertFalse(any("-185.24" in line for line in self.messages))

    def test_dashboard_is_complete_in_one_frame_and_fits_the_viewport(self):
        ansi = fdg.re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
        for width, height in ((80, 24), (60, 20), (40, 18), (80, 12)):
            for mask in (0, 0x100, 0x481, 0x87FF):
                with self.subTest(width=width, height=height, mask=hex(mask)):
                    frames = []
                    ui = fdg.Terminal(color="never", ascii_only=True, interactive=True,
                                      width=width, height=height, out=frames.append)
                    words = dict(self.fake.words)
                    words[1] = mask
                    fdg.render_dashboard(self.client, words, "resolution", ui)
                    self.assertEqual(len(frames), 1)
                    self.assertNotIn("\033[", frames[0])
                    lines = ansi.sub("", frames[0]).splitlines()
                    self.assertLessEqual(len(lines), height - 2)  # Keep the input prompt on-screen.
                    self.assertTrue(all(len(line) < width for line in lines))
                    flattened = " ".join(" ".join(line.strip(" |+-").split()) for line in lines)
                    self.assertIn("[Q] Quit", flattened)
                    self.assertIn("ERRORS & WARNINGS", flattened)
                    if mask:
                        for bit, name in enumerate(fdg.WARNINGS):
                            if mask & (1 << bit):
                                self.assertIn(name, flattened)
                        self.assertNotIn("No errors", flattened)
                    else:
                        self.assertIn("No errors", flattened)

    def test_dashboard_reads_new_terminal_size_on_each_refresh(self):
        with patch.object(fdg.os, "get_terminal_size", return_value=fdg.os.terminal_size((100, 40))) as size:
            frames = []
            ui = fdg.Terminal(color="never", ascii_only=True, interactive=True, out=frames.append)
            fdg.render_dashboard(self.client, self.fake.words, "resolution", ui)
            self.assertEqual((ui.width, ui.height), (99, 40))
            size.return_value = fdg.os.terminal_size((40, 18))
            fdg.render_dashboard(self.client, self.fake.words, "resolution", ui)
            self.assertEqual((ui.width, ui.height), (39, 18))
            lines = fdg.re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", frames[-1]).splitlines()
            self.assertTrue(all(len(line) <= 39 for line in lines))
            self.assertLessEqual(len(lines), 16)

    def test_dashboard_stdout_is_written_and_flushed_once(self):
        stream = Mock()
        stream.encoding = "utf-8"
        stream.fileno.side_effect = OSError("No terminal")
        with patch("sys.stdout", stream):
            ui = fdg.Terminal(color="never", ascii_only=True, interactive=True, width=80, height=24)
            fdg.render_dashboard(self.client, self.fake.words, "resolution", ui)
        stream.write.assert_called_once()
        stream.flush.assert_called_once()
        self.assertIn("[Q] Quit", stream.write.call_args[0][0])
        self.assertNotIn("\033[", stream.write.call_args[0][0])

    def test_default_edit_form_is_complete_and_has_no_cursor_controls(self):
        frames = []
        ui = fdg.Terminal(interactive=True, width=80, height=24, out=frames.append)
        fdg.render_edit_help(self.client, fdg.BY_KEY["flow_unit"], self.fake.words,
                             None, "resolution", ui)
        self.assertEqual(len(frames), 1)
        self.assertNotIn("\033", frames[0])
        self.assertIn("EDIT SETTING", frames[0])
        self.assertIn("Current:", frames[0])
        self.assertIn("0  m3/h", frames[0])
        self.assertIn("1  L/min", frames[0])
        self.assertIn("2  CFM", frames[0])
        self.assertLessEqual(len(frames[0].splitlines()), 22)

    def test_plain_settings_to_edit_transition_outputs_the_whole_form(self):
        process = subprocess.run(
            [fdg.sys.executable, str(Path(fdg.__file__).resolve()), "--demo"],
            input="s\n1\n\n\nq\n", capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn("\033", process.stdout)
        start = process.stdout.index("Select setting > ")
        end = process.stdout.index("New value > ", start)
        form = process.stdout[start:end]
        self.assertIn("EDIT SETTING", form)
        self.assertIn("0  m3/h", form)
        self.assertIn("1  L/min", form)
        self.assertIn("2  CFM", form)

    def test_live_trend_includes_recent_flow_and_keeps_csv_decoding(self):
        self.assertEqual(len(fdg.sparkline([fdg.Decimal(1)] * 100, 20, False)), 20)
        self.assertEqual(fdg.sparkline([fdg.Decimal(0), fdg.Decimal(10)], 10, False), ".@")


if __name__ == "__main__":
    unittest.main()
