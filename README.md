# FD-G terminal configuration tool

Run this on the Linux box that can reach the Keyence FD-G. It uses the existing
**Modpoll 3.4** executable and **Python 3.9 or later**, with no Python packages to install.
Supply the meter address with `--host` or the `FDG_HOST` environment variable.
Without either, the default is localhost (`127.0.0.1`), TCP port `502`, unit ID `1`.
The published source contains no customer meter address.

## Start

Copy `fdg_tool.py` onto the Linux box, then run:

```bash
export FDG_HOST=YOUR_METER_IP
python3 fdg_tool.py
```

Version **1.1.1** shows all settings in a single list with numbered choice legends.
It includes a colored dashboard with measurement cards, a live flow trend, and
a settings editor with unit-aware decimal examples. Colors and Unicode
borders turn on automatically in a compatible SSH terminal. No packages are required.

The dashboard refreshes as a complete frame and checks the current terminal size
each time. It switches to a compact layout when the full view would scroll out
of the window. The **ERRORS & WARNINGS** area lists the meter's decoded errors
and warnings, including reverse flow, or displays **No errors** when the status
word is zero. A failed read displays an unavailable status instead of reusing an
old error state. Modpoll subprocesses do not share the menu's keyboard input.

The menu displays current flow, raw flow, peak/bottom hold, integrated flow,
leak flow, pressure, conversion basis, stability, output states, initialization,
error/warning names (including reverse flow), and operation statuses.

- `w`: Live dashboard and trend, refreshed approximately every second. Ctrl+C returns to the menu.
- `s`: Open **Settings & edit**. Type a setting number to edit it directly.
- `e`: Alias for `s`, retained for users of version 1.0.
- `b`: Save a settings snapshot.
- `m`: Select the FD-G size variant for pipe-size labels and validation.
- Enter: Refresh the status display.
- `q`: Exit.

In **Settings & edit**, the table groups settings by flow, pipe installation,
gas/conversion, and display units. All 22 editable settings appear in one list;
use terminal scrollback if the list is taller than the window. The **ENTER / CHOICES**
column shows the decimal pattern and unit for numbers, or the actual numbered
choices, such as `0=m3/h, 1=L/min, 2=CFM`. Longer choices wrap without being cut off.
Type a setting number, manual register address, or setting name.
Use `r` to refresh or Enter to return to the dashboard.

For a numeric setting, the edit form shows the current unit, allowed range,
maximum decimal places, smallest step, and an example with its raw encoding.
Type an ordinary engineering value, such as **110.00** when the form says psi,
**60.30** for pipe diameter in mm, or **1.403** for specific heat ratio.
Do not add the unit to the number. An entry like `60.3` is also valid for a
two-decimal field; extra trailing zeroes do not change the value. Unsupported
precision stays in the form with an explanation, so you can correct the value.
For choices, the form lists codes and labels; either input is accepted.
Review the before/after values, then type `APPLY` to write and verify the setting.
Enter cancels. Typing `?` in the value prompt repeats the format instructions.

Select the actual size variant from the meter label before editing pipe size (196).
`FD-G25`, `FD-G50`, `FD-G80`, `FD-G125`, and `FD-G200` are supported, including
their L versions. The tool cannot identify that variant from the supplied register map.
Other settings can be edited without selecting a variant.

To preview the interface without a meter or Modpoll installation:

```bash
python3 fdg_tool.py --demo
```

Demo mode simulates raw register values in memory; it does not simulate gas physics
or the firmware's unit conversion behavior. Its changes disappear when the process exits.

## Commands over SSH

Global options go **before** the command, as in these examples:

```bash
# Current readings and status
python3 fdg_tool.py status

# Current settings
python3 fdg_tool.py settings

# Set the meter's flow unit to CFM (code 2); preview and confirmation follow
python3 fdg_tool.py set flow_unit CFM
python3 fdg_tool.py set 199 2

# Set response time, outer diameter, and pressure (separate examples)
python3 fdg_tool.py set response_time 2.5s
python3 fdg_tool.py set pipe_od 60.30
python3 fdg_tool.py set pipe_pressure 500.0

# Enter zero cut in the currently displayed flow unit; the form shows its step
python3 fdg_tool.py set zero_cut 2.5

# Explicit raw zero-cut integer (compatible with version 1.0's raw-count input)
python3 fdg_tool.py set zero_cut 180 --raw

# Explicit raw register input; 6030 means 60.30 mm for register 108
python3 fdg_tool.py set 108 6030 --raw

# A pipe-size example ONLY if the actual meter is an FD-G50 / FD-G50L
python3 fdg_tool.py --model FD-G50 set pipe_size 1

# Live display and a new CSV file; use a fresh filename on subsequent runs
python3 fdg_tool.py watch --csv flow-before.csv
python3 fdg_tool.py --interval 2 watch --count 30 --csv flow-after.csv

# Override connection or executable path
python3 fdg_tool.py --host YOUR_METER_IP --modpoll /usr/local/bin/modpoll status

# Terminal appearance controls (also respects the NO_COLOR environment variable)
python3 fdg_tool.py --color never --ascii

# Force the compact dashboard if preferred
python3 fdg_tool.py --compact

# Save all supported settings, or inspect the register catalog offline
python3 fdg_tool.py snapshot
python3 fdg_tool.py catalog
```

No setting changes merely by starting the tool. CFM is available as an edit;
the display initially uses whatever unit is currently configured on the meter.
Polling requests run sequentially. The interval is a target cadence; slow replies
can make a cycle take longer. Failed cycles display an error and do not reuse an
old flow reading. CSV timestamps and snapshot timestamps are UTC.

## Editable settings

All addresses below are the **zero-based relative addresses printed in the manual**.
The tool adds 1 only when building Modpoll commands and omits `-0` to work around
the supplied Modpoll 3.4 build rejecting reference 0. Thus manual register 199
becomes Modpoll `-r 200`, and manual register 0 becomes Modpoll `-r 1`.

| Manual register(s) | Setting name | Input |
|---|---|---|
| 25 | `flow_resolution` | Read-only: code 0/1/2/3 = 0.001/0.01/0.1/1 |
| 26 | `total_resolution` | Read-only: same resolution codes |
| 100 | `response_time` | 0=1s, 1=2.5s, 2=5s, 3=10s, 4=30s, 5=60s, 6=120s, 7=200s |
| 101-102 | `zero_cut` | Current flow unit; step from register 25 in resolution mode, whole units in integer mode |
| 106 | `pipe_material` | 0=Steel, 1=Stainless steel (SUS) |
| 107 | `advanced_pipe` | 0=OFF, 1=ON |
| 108 | `pipe_od` | 0.01-300.00 mm, increments of 0.01 mm |
| 109 | `pipe_thickness` | 0.10-30.00 mm, increments of 0.01 mm |
| 110 | `gas_type` | 0=Air, 1=Input value |
| 111 | `specific_heat_ratio` | 1.000-2.000, increments of 0.001 |
| 112 | `molecular_weight` | 1.00-100.00, increments of 0.01 |
| 113 | `span` | 0.100-10.000 x, increments of 0.001 |
| 116 | `reference_temp` | 0.0-100.0 in the current C/F unit, increments of 0.1 |
| 194 | `integration_direction` | 0=Standard, 1=Bi-directional |
| 195 | `flow_direction` | 0=To the cable connector, 1=From the cable connector |
| 196 | `pipe_size` | Model-specific code; select the actual size variant first |
| 197 | `piping_standard` | 0=--, 1=JIS, 2=ASTM, 3=DIN, 4=GB, 5=Other |
| 198 | `conversion` | 0=NTP, 1=STP, 2=OFF |
| 199 | `flow_unit` | 0=m3/h, 1=L/min, 2=CFM |
| 200 | `pressure_input` | 0=--, 1=Manual input, 2=Analog input |
| 201 | `pipe_pressure` | Current pressure unit; raw range 0-60000 |
| 207 | `additional_unit` | **0=ON, 1=OFF** |
| 208 | `pressure_unit` | 0=kPa, 1=bar, 2=psi |
| 209 | `temperature_unit` | 0=C, 1=F |

Enum settings accept numeric codes or their displayed labels. Numeric settings
accept engineering values by default. **Version 1.1 changes zero cut to engineering
input too**: with resolution 0.1, an input of `2.5` encodes as raw `25`. If you
previously entered a raw zero-cut count, add `--raw` to that CLI command.
`--raw` (CLI) or a `raw:` prefix (menu) accepts raw integers instead.
Values with unsupported precision are rejected instead of rounded.

Pressure scaling depends on register 208: raw x 0.1 kPa, raw x 0.001 bar,
or raw x 0.01 psi. Reference temperature is raw x 0.1 in register 209's unit.
The tool reads these units again before each edit.

Advanced pipe settings must be ON before changing outer diameter/thickness.
Dimensions must describe a positive internal diameter. Pipe size/standard can
cause the meter to update pipe dimensions automatically; the full settings
refresh reports and records such changes. Custom gas properties are effective
with Gas type = Input value. The reference temperature is used for STP and
manual pipe pressure is used with Manual input. These dependencies are displayed
when editing. Switching to Bi-directional integration also limits leakage,
target and I/O functions as documented on manual page 28.

Registers 202-203 (analog pressure scaling) and 220 (external operations/reset)
are outside the editable set. Selecting Analog input does not configure its
pressure scaling; the existing 202-203 settings must already be appropriate.

## Flow decimal interpretation

**Keep raw and displayed flow distinct until comparing with the meter's own display.**
The supplied manual has conflicting descriptions:

- Page 60 describes instantaneous flow with the decimal point removed.
- Page 73's Modbus table describes the decimal point and following digits removed.
- Page 74 provides read-only instantaneous/integrated resolution codes at 25/26.

The default `--flow-mode resolution` multiplies the signed raw instantaneous flow
by register 25's resolution, and unsigned integrated flow by register 26's
resolution. This interpretation is an inference from the resolution registers
and page 60. The raw instantaneous integer is always printed alongside it.
Peak, bottom and leak flow use register 25's resolution in this mode.

For example, the supplied words `B7A4 FFFF` decode to raw **-18524**. If register
25 contains code 1 (resolution 0.01), resolution mode shows **-185.24**, in the
meter's current flow unit. We have not yet read the real meter's register 25.

If comparison confirms the Modbus value is already in whole engineering units,
use the explicit unscaled interpretation:

```bash
python3 fdg_tool.py --flow-mode integer
python3 fdg_tool.py --flow-mode integer watch
```

Both modes retain raw data in CSV logs. Unknown resolution codes retain the raw
value and mark the scaled display unavailable. Zero cut follows the same selected
flow interpretation: raw x register 25's resolution in resolution mode, or whole
engineering units in integer mode. The preview always shows its encoded raw value.
Until flow scaling has been compared with a trusted reading, use explicit raw
input for zero cut when you already know the register integer you want to write.
CFM labels follow register 199; NTP/STP basis is reported separately. A unit
change does not trigger extra writes to response, cut-off, pressure, or other settings.

## Applying and reviewing changes

1. Choose one setting. The tool reads the full supported configuration and shows
   the existing value, requested value, raw integer, and relevant dependency notes.
2. Type `APPLY` to confirm that specific change. Enter cancels it.
3. The tool rechecks that configuration has not changed since the preview,
   then saves a JSON snapshot and write-attempt audit entry under `fdg_logs/`.
   If saving fails, it does not write to the meter.
4. It writes the setting. Registers 101-102 are always supplied together in one
   consecutive-register write, lower word first. Other settings write one word.
   Both FC06 and FC16 are supported by this meter; Modpoll handles the write request.
5. It reads the setting back and verifies the exact raw integer. It then saves
   an updated snapshot and reports other supported settings changed by firmware.
   A refresh failure is reported separately from a successful target verification.

`changes.jsonl` records attempted and verified changes, mismatches, and unknown
outcomes. If the write response is lost, the tool still checks read-back. It
never automatically retries a write or rolls back, because the first write may
already have taken effect. For an unknown result, inspect current settings and
the saved snapshot before choosing another edit.

Snapshots contain raw register values and interpreted values for review. There
is no bulk restore command: review old values and apply individual changes so
unit, pipe and other dependencies stay visible. No separate save/commit register
is specified in the supplied Modbus section; the tool claims immediate read-back
verification only. Retention after a power cycle has not been tested.

## Source and verification

Register definitions are from the supplied KEYENCE FD-G User's Manual,
`AS_117098_FD-G_UM_160100_WW_GB_2105_3 (1).pdf`, pages 71-79. Pipe/gas/conversion
dependencies are on pages 28-30. Observed Modpoll behavior and trace syntax come
from the Modpoll 3.4 output supplied in the conversation.
The vendor also provides [Modpoll command documentation](https://www.modbusdriver.com/modpoll.html).

The tool has offline behavior tests for addressing, parsing, signed decoding,
word order, the editable-register boundary, input scaling, cancellation, stale
configuration, snapshots, failed writes, verification and logging. The interface
tests cover editing directly from Settings, decimal examples in the current unit,
input correction, read-only fields, the complete settings list and numbered choices,
model selection, terminal widths,
color controls and cursor restoration after failed reads. Dashboard checks cover
complete single-frame redraws, resizing, short/narrow viewports, and both clear
and active meter error states. Run them with:

```bash
python3 -m unittest -v
```

The tool has not connected to the customer meter from this development workspace.
Verify a read-only `status` run on the Pi first, then compare flow decimal scaling
against the meter's display or an existing trusted reading before using flow to
evaluate setting changes.
