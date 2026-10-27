# Drink Refill Monitor 🥤

An Arduino App for the **Arduino UNO Q** that weighs your cup on a load-cell
coaster and tells you when your drink is **running low** or **empty**.

- **On the board:** the 8×13 LED matrix shows a mug that drains as you drink and
  flashes `!` when it's empty; the RGB status LED goes green → amber → blinking red.
- **In the browser:** a live dashboard at `http://<board-ip>:7000` with the fill
  level, a 15-minute history, calibration steps and alert settings.
- **On your phone (optional):** a push notification through [ntfy](https://ntfy.sh)
  or a Discord webhook when the cup runs dry, with reminders until you refill.
- **For AI agents:** an [MCP](https://modelcontextprotocol.io) server at
  `http://<board-ip>:7000/mcp`, so Claude Code or any MCP-capable agent can ask
  "is my cup empty?", track how much you've drunk today, and nudge you.

It knows the difference between *empty* and *picked up for a sip*, and it spots
refills automatically — whether you pour into the cup on the coaster or carry it
to the kitchen and back.

**No load cell yet?** The app runs on a bare UNO Q: it falls back to a built-in
coaster simulator you drive from a terminal (`tools/coaster.py`). See
[No hardware? Use the simulator](#no-hardware-use-the-simulator).

---

## Why weight?

| Approach | Problem |
|---|---|
| Liquid-level probe in the cup | Something in your drink; needs cleaning; one cup only |
| IR / ultrasonic from above | Fooled by ice, foam, straws, clear liquids |
| **Load cell under a coaster** | **Works with any cup and any drink, nothing touches the liquid** |

The app remembers the weight of your empty cup, so *weight − cup = drink left*.

---

## Hardware

| Part | Notes |
|---|---|
| Arduino UNO Q | 2 GB or 4 GB |
| Bar load cell, 1 kg or 5 kg | 1 kg gives finer resolution for mugs; use 5 kg for big tumblers (cup + drink must stay under the rating) |
| HX711 load-cell amplifier module | The common green/red breakout boards work |
| Two rigid plates + spacers/screws | Coaster top and base; the bar is clamped between them, cantilever-style |
| *Optional:* active buzzer (3.3 V) | Beeps when the cup empties |

### Wiring

| HX711 pin | UNO Q pin |
|---|---|
| VCC | **3.3V** |
| GND | GND |
| DT (DOUT) | D3 |
| SCK | D2 |

Load cell → HX711: red → E+, black → E−, white → A−, green → A+ (colours vary by
vendor; if weight reads *negative*, it still works — calibration handles the sign).

Optional buzzer: `+` → D8, `−` → GND. No buzzer? Set `ENABLE_BUZZER` to `0` in
`sketch/sketch.ino` (leaving it at 1 is harmless, D8 just toggles).

```
   ┌───────── coaster top ─────────┐
   │                               │
   └──spacer──[==== load cell ====]│     screw the bar to the top plate at one
              │                    │     end and to the base at the other, with
   ┌──────────┴── base ────────────┘     spacers so the middle can flex
```

---

## Install

Copy this folder onto the board as `~/ArduinoApps/drink-refill-monitor`, then it
shows up under **My Apps** in Arduino App Lab.

```bash
# from your computer (replace the IP with your board's)
scp -r drink-refill-monitor arduino@192.168.1.50:~/ArduinoApps/
```

Then either press **Run** in App Lab, or on the board:

```bash
arduino-app-cli app start ~/ArduinoApps/drink-refill-monitor
arduino-app-cli app logs  ~/ArduinoApps/drink-refill-monitor --follow
```

App Lab compiles and flashes `sketch/` to the microcontroller and starts
`python/main.py` on Linux with the `web_ui` brick. Open
`http://<board-ip>:7000`.

---

## First-time setup (2 minutes, in the dashboard)

1. **Tare** — clear the coaster, press *Tare*.
2. **Calibrate** — put something of known weight on it (a sealed 500 ml water
   bottle is ≈ 520 g; a kitchen scale is better), type the grams, press *Calibrate*.
3. **Set empty cup** — put your empty cup down, wait for "Reading steady",
   press *Set empty cup*.

Fill the cup. That's it. Calibration is saved to `data/settings.json` and
survives restarts. Switching cups? Just do step 3 again.

---

## How detection works

```
HX711 ──raw counts──► sketch.ino ──Bridge.notify("hx711_sample")──► main.py
                                                                       │
                                                         drink_monitor.py
                                    median filter → grams → stable? → liquid = grams − cup
                                                                       │
   LED matrix + RGB LED + buzzer ◄──Bridge.call("set_display"/"beep")──┤
   Dashboard (socket.io)        ◄──WebUI.send_message("status"/"alert")┤
   Phone push (ntfy/Discord)    ◄──notifier.send()─────────────────────┘
```

- **Stable readings only.** Weight must hold within ±3 g for 1.5 s before it
  counts, so pouring, bumping, or stirring never triggers anything.
- **Lifted ≠ empty.** If the coaster suddenly reads ~0 g, the cup is in your
  hand. Status becomes *Cup lifted*; after 90 s with no cup it becomes *No cup*.
- **Refill detection.** A stable jump of ≥ 30 g (on the coaster, or after a
  lift) resets "full" to the new level.
- **States:** `ok` → `low` (≤ 25 % of the last fill) → `empty` (≤ 12 g of drink
  left). A few grams of hysteresis stop it flickering at the boundaries.
- **Alerts:** one on *low* (dashboard only), one on *empty* (dashboard, buzzer,
  phone), then a reminder every 10 minutes until you refill or press *Snooze*.
  Putting down a cup that's already empty doesn't alert — that's not news.

All thresholds are editable in the dashboard (*Alerts* card) or as defaults in
`Settings` in `python/drink_monitor.py`.

### Phone notifications (optional)

Install the ntfy app, subscribe to a hard-to-guess topic like
`my-drink-7f3k9q`, and paste `https://ntfy.sh/my-drink-7f3k9q` into
*Phone notification URL*. Press *Send test alert* to check. A Discord webhook
URL works too; any other URL receives a JSON POST `{title, message, event}`.

---

## No hardware? Use the simulator

Run the app on a bare UNO Q (or with the load cell unplugged). If the board
hasn't heard from a load cell ~6 seconds after starting, it switches to a
**virtual coaster** and says so: the dashboard shows a *Simulated coaster*
badge, the log prints `No load cell found — using the simulated coaster`, and
AI agents see `"simulated": true`.

Everything else is real: the detection logic, the dashboard, the LED matrix and
RGB LED, alerts, phone notifications (titled `[demo] …`) and the MCP tools all
run on the simulated readings. The virtual coaster comes pre-calibrated, starts
with a 300 g cup holding 350 g of drink, and keeps its own calibration
(`data/settings.sim.json`) and intake log, so demo data never mixes with real data.

### Driving it from a terminal

`tools/coaster.py` is a single standard-library Python file. Run it on your
laptop pointing at the board, or on the board itself over SSH.

```bash
export DRINK_BOARD=192.168.1.50          # your board's IP (or uno-q.local)

python3 tools/coaster.py demo --wait     # ~1 min show: pour → sips → low → empty → refill
python3 tools/coaster.py sip 40 --wait   # pick up, drink 40 g, put back, follow along
python3 tools/coaster.py watch           # live status line (Ctrl-C to stop)
python3 tools/coaster.py                 # interactive shell: type commands, `help`, `quit`
```

| Command | What happens on the virtual coaster |
|---|---|
| `pour [g] [s]` | Pour drink into the cup on the coaster (default 300 g over 3 s) |
| `sip [g] [hold_s]` | Lift the cup, drink, set it back down (default 30 g, held 2 s) |
| `straw [g] [s]` / `drain [s]` | Drink without lifting / finish the whole drink |
| `lift` / `down` | Pick the cup up and hold it / put it back |
| `refill [g] [away_s]` | Carry the cup away and come back with it filled |
| `remove` / `place [cup_g]` | Take the cup away / put it back (with a weight: a new empty cup) |
| `set <g>` | Teleport the drink level, no animation |
| `weight <g>` | Put a calibration object on the coaster (`weight 0` removes it) — lets you demo the dashboard's calibration steps |
| `fault [on\|off]` | Pretend the load cell is unplugged |
| `noise <g>` | Sensor noise (default 0.4 g) |
| `wait <s>` / `stop` | Queue a pause / cancel everything queued |
| `demo [sip_g]` | The scripted one-minute show |
| `status` / `help` | What the coaster and the monitor think / the full list |
| `mode <sim\|real\|auto>` | Force the simulator, force the real load cell, or choose automatically (remembered across restarts) |

Actions take real time (a sip lifts the cup for 2 s) and queue up, so you can
type `pour 300`, `sip`, `sip`, `refill` back to back. `--wait` (or no `&` in the
shell) follows along and prints each change the monitor sees. In the shell,
end a command with `&` to get the prompt back immediately.

### How the automatic choice works

| `DRINK_SENSOR` / `mode` | Behaviour |
|---|---|
| `auto` (default) | Real load cell if it answers within ~6 s of start-up, simulator otherwise. If a load cell is plugged in later, it takes over. A load cell that stops answering mid-run shows *Sensor problem* — it never silently falls back to fake data. |
| `real` | Always the load cell. |
| `sim` | Always the simulator, even with a load cell attached. |

The simulator endpoint (`/api/sim`) has the same trust level as the dashboard:
anyone on your network can use it, but web pages on other sites can't.

---

## Use it from an AI agent (MCP)

The app serves an MCP endpoint from the same web server as the dashboard — no
extra install, no extra port:

```
http://<board-ip>:7000/mcp          (Streamable HTTP, bearer token required)
```

Open the dashboard's **Connect an AI agent** card for the exact URL, the token
(created on first run, stored in `data/mcp_token.txt`) and copy-ready config.

### Tools

| Tool | What the agent gets / does |
|---|---|
| `get_drink_status` | Live state (`ok` / `low` / `empty` / `lifted` / `no_cup` / …), grams and % left, last refill time, how long it's been empty, current drinking pace, and an estimate of minutes until empty. Includes a one-line `summary`. |
| `get_drink_history` | Level over the last 1–60 min (≤ 60 points) plus events: refills, low/empty alerts, cup removed, snoozes, nudges. |
| `get_intake_summary` | Grams drunk today, today by hour, and per-day totals for up to 30 days (in the user's time zone if the agent passes `utc_offset_minutes`). |
| `snooze_refill_reminders` | Silences the repeating "still empty" reminder until the next refill. |
| `update_alert_settings` | Changes the low %, empty grams and reminder interval. |
| `nudge_drinker` | Beeps the coaster and shows a dashboard banner (optionally a phone push). Rate-limited to once a minute. |

Read-only tools are annotated `readOnlyHint`, and the server sends
instructions telling the model how to interpret the numbers (percent is of the
last fill, grams ≈ ml, "lifted" ≠ empty). Every agent call shows up under
**Recent agent activity** on the dashboard.

### Connecting

**Claude Code** (or any client that speaks Streamable HTTP):

```bash
claude mcp add --transport http drink-coaster http://192.168.1.50:7000/mcp \
  --header "Authorization: Bearer <token>"
```

Then ask things like *"how much water have I had today?"* or *"remind me to
refill when my cup's empty"*.

**Apps that only launch local (stdio) servers** — use the bundled bridge, which
is a single standard-library Python file you can copy to any computer:

```json
{
  "mcpServers": {
    "drink-coaster": {
      "command": "python3",
      "args": ["/path/to/tools/mcp_stdio_bridge.py", "http://192.168.1.50:7000/mcp"],
      "env": { "DRINK_MCP_TOKEN": "<token>" }
    }
  }
}
```

**Agent frameworks** (OpenAI Agents SDK, LangChain via `langchain-mcp-adapters`,
the MCP Python/TypeScript SDKs, …): point their Streamable HTTP MCP client at
the URL above with an `Authorization: Bearer <token>` header.

> Cloud-hosted connectors (e.g. adding a connector on claude.ai) run on the
> provider's servers and can't reach a board on your home network. Use a local
> agent, or expose the board through a tunnel you trust.

### Protocol notes

- Speaks both MCP eras: the stateless **2026-07-28** revision (`server/discover`,
  per-request `_meta`, `Mcp-Method`/`Mcp-Name` header checks) and the
  **2025-03-26 → 2025-11-25** handshake revisions (`initialize`, `ping`, batches).
  Tested with the official MCP Python SDK 2.3 (both modes) and 1.30.
- Plain JSON responses (no SSE), no sessions. `GET`/`DELETE` → 405.
- Security: bearer token on every request; browser requests from other sites
  are refused via `Origin` checks. The dashboard itself has no login, so anyone
  who can open it on your network can read the token — treat your LAN as the
  trust boundary. **New token** on the card revokes the old one. Set
  `DRINK_MCP_TOKEN` in the environment to pin your own.

---

## Project layout

```
drink-refill-monitor/
├── app.yaml                 App Lab descriptor (web_ui brick, port 7000)
├── sketch/
│   ├── sketch.ino           MCU: HX711 sampling, LED matrix, RGB LED, buzzer
│   ├── hx711_lite.h         Dependency-free HX711 driver
│   └── sketch.yaml
├── python/
│   ├── main.py              Linux: Bridge + WebUI + MCP glue, event loop, persistence
│   ├── drink_monitor.py     Detection logic (pure Python, no Arduino imports)
│   ├── intake.py            How much was drunk, per hour/day
│   ├── mcp_server.py        Dependency-free MCP endpoint (Streamable HTTP)
│   ├── agent_tools.py       The six tools agents see
│   ├── sim_coaster.py       The virtual coaster used when there's no load cell
│   └── notifier.py          ntfy / Discord / generic webhook
├── assets/                  Dashboard (index.html, app.js, style.css, libs/)
├── tests/                   Detection-logic, simulator, MCP protocol and tool tests
├── tools/
│   ├── coaster.py           CLI for the board's coaster simulator
│   ├── simulate.py          Offline run of the detection logic — no board needed
│   └── mcp_stdio_bridge.py  stdio ⇄ HTTP bridge for local-only MCP clients
└── data/                    Created at runtime: settings*.json, intake.json, mcp_token.txt, sensor_mode.txt
```

## Develop without the board

```bash
python3 -m unittest discover -s tests -v   # 60 tests
python3 tools/simulate.py                  # timeline of a simulated session
```

Sample output:

```
t=    7.5s  setup_done  {'cup_g': 309.9}
t=   12.2s  refilled    {'liquid_g': 400.4}
t=  395.6s  low         {'message': 'Running low — about to need a refill.'}
t=  515.0s  empty       {'message': 'Your drink is empty — time for a refill!'}
t=  635.0s  reminder    {'message': 'Still empty — grab a refill when you can.'}
t=  750.9s  refilled    {'liquid_g': 380.1}
t=  849.7s  cup_removed
t=  854.5s  total_drunk {'grams': 400.4}
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard says **Sensor problem**, matrix flashes `!` | HX711 isn't answering: check 3.3V/GND, and that DT→D3, SCK→D2 aren't swapped |
| Raw reading never changes when you press the coaster | Load-cell wires to E+/E−/A+/A− are wrong, or the bar isn't actually flexing (check spacers) |
| "Barely any change from the empty reading" during calibration | Same as above, or the known weight wasn't on the coaster |
| Reading drifts by several grams over hours | Normal for cheap load cells; re-tare occasionally, or raise *“Empty” below* to 15–20 g |
| Matrix shows a blinking `?` | Python isn't sending updates — the app is stopped or crashed; check `arduino-app-cli app logs … --follow` |
| Bridge calls fail after editing the app | In the App Lab terminal: `arduino-app-cli system cleanup`, then restart the app |
| No chime in the browser | Browsers only allow sound after you've clicked the page once |
| Dashboard shows *Simulated coaster* but the load cell is wired | It wasn't heard at start-up. Check wiring, then `coaster.py mode auto` (or `mode real`) — or just restart the app |
| `coaster.py` says it can't reach the board | Use the board's IP and the same network (a phone hotspot is the safe bet at events); the app must be running |
| Agent gets 401 | Token is wrong or was regenerated; copy it again from the dashboard |
| Agent can't connect at all | Use the board's IP (not a cloud connector), port 7000, path `/mcp`, and make sure the app is running |
