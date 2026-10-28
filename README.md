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

The menu displays current flow, raw flow, peak/bottom hold, integrated flow,
leak flow, pressure, conversion basis, stability, output states, initialization,
error/warning names (including reverse flow), and operation statuses.

- `w`: Live readings, refreshed approximately every second. Ctrl+C returns to the menu.
- `s`: Read all supported settings and their current values.
- `e`: Edit a setting using its name or the manual's register address.
- `b`: Save a settings snapshot.
- `m`: Select the FD-G size variant for pipe-size labels and validation.
- Enter: Refresh the status display.
- `q`: Exit.

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

# Enter the combined raw zero-cut integer, not its upper/lower words separately
python3 fdg_tool.py set zero_cut 180

# Explicit raw register input; 6030 means 60.30 mm for register 108
python3 fdg_tool.py set 108 6030 --raw

# A pipe-size example ONLY if the actual meter is an FD-G50 / FD-G50L
python3 fdg_tool.py --model FD-G50 set pipe_size 1

# Live display and a new CSV file; use a fresh filename on subsequent runs
python3 fdg_tool.py watch --csv flow-before.csv
python3 fdg_tool.py --interval 2 watch --count 30 --csv flow-after.csv

# Override connection or executable path
python3 fdg_tool.py --host YOUR_METER_IP --modpoll /usr/local/bin/modpoll status

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
| 101-102 | `zero_cut` | One combined raw unsigned integer, 0-999999 |
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
accept engineering values by default, except `zero_cut`, which explicitly uses
raw counts. `--raw` (CLI) or a `raw:` prefix (menu) accepts raw integers instead.
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
value and mark the scaled display unavailable. Zero cut remains a raw-count edit
in either mode so this documentation ambiguity cannot silently scale a write.
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
configuration, snapshots, failed writes, verification and logging. Run them with:

```bash
python3 -m unittest -v
```

The tool has not connected to the customer meter from this development workspace.
Verify a read-only `status` run on the Pi first, then compare flow decimal scaling
against the meter's display or an existing trusted reading before using flow to
evaluate setting changes.
