# Bedtime Bridge for Home Assistant

> 🌐 **[See the website](https://2tk785fryr-tech.github.io/bedtime-bridge-homeassistant/)**

**Up on time, every time.**

The Home Assistant side of the [Bedtime Bridge](https://github.com/2tk785fryr-tech/bedtime-bridge-showcase)
iPhone app. The app knows your work schedule (a regular 9-to-5 or shifts) from your calendar, your
week or a rotation, and your appointments from your calendar, and sets your wake-up alarms. This
integration lets your house take part: it knows when you snooze, stop, nap, wind down and go to
bed, turns switches on and off for your own automations, and works out backstops that step in
with lights and speakers if you're still in bed when you should be up.

This repo holds only the latest version of the integration (**0.4.12.9**), plus generic
blueprints you can import with one tap.

**New with app 0.6.0 (integration 0.4.12.9):** the backstops work with My week and rotations too,
not only a work calendar. When Home Assistant gets drive times from Google (**Drive times from**),
the phone no longer asks Google for the same morning, and a daily limit under **Configure → Keys**
keeps the requests down. Update from the Marketplace.

---

## What you need

- Home Assistant 2025.1 or later.
- The Bedtime Bridge app on your iPhone, with **Home Assistant** chosen as your home in the app.
- The Home Assistant Companion app on the same iPhone (for notifications and your location).

---

## 1. Install the integration

### From the Marketplace (recommended)

1. In Home Assistant, open **Marketplace** → **⋮** (top right) → **Custom repositories**.
2. **Repository:** `https://github.com/2tk785fryr-tech/bedtime-bridge-homeassistant`,
   **Type:** Integration → **Add**.
3. Search for **Bedtime Bridge** → **Download**.
4. Restart Home Assistant.

Updates then show up in the Marketplace like any other.

### By hand

Copy `custom_components/bedtime_bridge` from this repo into your Home Assistant
`config/custom_components/` folder, then restart Home Assistant.

---

## 2. Add it and pair your phone

1. **Settings → Devices & services → Add integration → Bedtime Bridge.**
2. Pick **you** (your person, or your phone's tracker) so it knows when you're home, and your
   phone's notify action (it suggests your iPhone's).
3. The last step shows a **Pair Bedtime Bridge** link. Open that step in the Home Assistant app
   on your iPhone and tap the link: both addresses are saved in Bedtime Bridge. If the link
   doesn't open, copy it into the app's address field (**Settings → Home Assistant** in
   Bedtime Bridge, or Smart home › Home Assistant when Settings is grouped).

You can see the pairing link again under the integration's **Configure**. Everything else in
the options is optional.

**Keep the link and addresses private.** The long code at the end of each address is the only
key, as with any Home Assistant webhook. Anyone who has it can see your alarm and shift times and
whether you're home, and can send alarm updates, run the wake and bedtime routines, disarm,
change the integration's settings, reset the alarms and backstops, or write reports. If you think
it got out, delete the integration and add it again: that makes new addresses, but you set it up
and pair the phone again.

---

## 3. What the integration makes for you

A new install makes its own switches, so there's nothing to create by hand. Each one keeps its
state across restarts. Your automations (and the blueprints below) can watch them.

| Switch | On when | Use it for |
|---|---|---|
| `switch.bedtime_bridge_bedtime` | You start your bedtime routine in the app | Your bedtime automation (lights down, lock up) |
| `switch.bedtime_bridge_snooze` | You snooze | A gentle light ramp while you snooze |
| `switch.bedtime_bridge_stop` | You stop the first alarm | Your wake-up (lights, curtains) |
| `switch.bedtime_bridge_early_wake_up` | You end the alarms before they ring | The same wake-up, started early |
| `switch.bedtime_bridge_sleep_in` | Your first alarm is after the curtains would open, or you skip a morning | Keep curtains and lights from waking you |
| `switch.bedtime_bridge_awake_for_day` | Good Morning, or when your alarms are done (off at bedtime) | Your house's day mode |
| `switch.bedtime_bridge_napping` | A nap at home is running | The nap blueprint: a dark, quiet room |
| `switch.bedtime_bridge_winding_down` | From wind-down until you go to bed | The wind-down blueprint: warm, dim lights |
| `switch.bedtime_bridge_exit_latch` | Your bedroom door sensor opens (off at bedtime) | Tells the integration and the backstop you're really up |
| `select.bedtime_bridge_shift_override` | Auto, Off, or one of your shift types | Off for a day off: skipping a shift in the app sets it |

Snooze, Stop, Early wake-up and Bedtime work like buttons: if one is already on, it turns off
and on again, so automations that start on "on" run each time. Bedtime Bridge turns Snooze, Stop,
Early wake-up and Awake For Day off again at bedtime.

It also adds sensors (next alarm, last plan, leave-by, the backstops' early, deadline and late
times and their status, snoozing, wind-down) and a number for each setting the app reads.

**Briefings.** The morning and bedtime briefings are on `sensor.bedtime_bridge_briefing`
(attributes `morning` and `bedtime`) and come as a `bedtime_bridge` event with type
`morning_briefing` or `bedtime_briefing` (`text`, `away`, and for the morning `quiet`). To speak
one, trigger your automation on that event and say `trigger.event.data.text`. Don't read the
sensor when the alarm is stopped: the morning briefing is built just after Stop, so at that moment
the sensor still holds the last one. The bedtime briefing is announced once a night.

### Already have your own helpers?

In **Settings → Devices & services → Bedtime Bridge → Configure**, each helper has a choice:
**Bedtime Bridge makes it** or **Use mine**. With Use mine, pick a toggle helper, a switch, or a
binary sensor (a binary sensor is only read, never changed). An existing setup keeps the helpers
it already picked.

For Awake For Day you can say what your helper being **on means**. With an "Asleep for the
night" helper, choose **Asleep**: Bedtime Bridge reads it the other way round, and at Good
Morning turns it off instead of turning Awake on.

---

## 4. What else you need

Tick off what you want. Only the first two are needed for alarms and pairing; the rest turn on
more of the house.

- [ ] **The Companion app's notify action** for your iPhone (e.g. `notify.mobile_app_your_phone`):
  picked when you add the integration.
- [ ] **You:** your `person` entity, so the house only acts when you're home.
- [ ] **A bed sensor** for your side of the bed (optional): tells whether you're really up when
  you stop an alarm, and drives the backstop. **Configure → Bedroom and bed.**
- [ ] **A bedroom door sensor** (optional): turns the exit latch on when you leave the bedroom.
  **Configure → Bedroom and bed.**
- [ ] **Your work calendar** and a **weather** entity (optional): first day back, and bad-weather
  extra time. **Configure → Shifts and calendar.**
- [ ] **Your phone's battery sensors** (optional): the "plug in your phone" reminder.
  **Configure → Phone.**
- [ ] **A text-to-speech entity and speakers** (optional): for the backstop's announcement.
- [ ] **The blueprints below**: import the ones you want and make an automation from each.

---

## 5. Blueprints (tap to import)

Each blueprint is generic: nothing is filled in for you. Tap **Import**, then **Create
automation** and pick your own lights, speakers and sensors. Leave optional fields empty to skip
that step.

| Blueprint | What it does | What to pick | Import |
|---|---|---|---|
| **Wake deadline backstop** (recommended) | Still in bed as your get-up deadline nears? It steps up: a lamp and a push, then a critical push, an announcement and full lights, until you're up. Silent on every normal morning. | The integration's backstop sensors: early, deadline and late (`sensor.bedtime_bridge_ladder_early`, `sensor.bedtime_bridge_ladder_deadline`, `sensor.bedtime_bridge_ladder_late`) and status (`sensor.bedtime_bridge_ladder_status`); your bed sensor; `switch.bedtime_bridge_exit_latch`; your notify action; lamps, lights, speakers and text-to-speech if you want them. | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2F2tk785fryr-tech%2Fbedtime-bridge-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fbedtime_bridge%2Fbedtime_bridge_wake_deadline_backstop.yaml) |
| **Alarm event** | Runs your actions when the app reports an alarm event: snoozed, stopped, alarms planned or cancelled, leave-by updated. | The event, and your actions. One automation per event. | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2F2tk785fryr-tech%2Fbedtime-bridge-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fbedtime_bridge%2Fbedtime_bridge_alarm_event.yaml) |
| **Missing plan** | Warns you if a work day is coming but tonight's alarms weren't set. Checks once you've been in bed a while. | Your bed sensor, the integration's Last plan sensor, a shift sensor (below), your notify action. | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2F2tk785fryr-tech%2Fbedtime-bridge-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fbedtime_bridge%2Fbedtime_bridge_missing_plan.yaml) |
| **Nap** | When a nap starts in a room, closes its curtains and turns off its lights, then puts them back after. Add the room's bed or presence sensors so a couch nap leaves the bedroom alone. | `switch.bedtime_bridge_napping`, the room's sensors, curtains and lights. One automation per room. | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2F2tk785fryr-tech%2Fbedtime-bridge-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fbedtime_bridge%2Fbedtime_bridge_nap.yaml) |
| **Wind-down** | Warm, dim lights when wind-down starts (only the ones already on), curtains closed, anything else you add. Leaves the lights for your bedtime routine when it ends. | `switch.bedtime_bridge_winding_down`, your lights and curtains. | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2F2tk785fryr-tech%2Fbedtime-bridge-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fbedtime_bridge%2Fbedtime_bridge_wind_down.yaml) |
| **Snooze fade** | Brings your bedside light up slowly from near-dark while you snooze, and fades it back down if you go back to sleep. Runs before sunrise and a while after, or later on sleep-in mornings. | `switch.bedtime_bridge_snooze`, your bedside lights, `switch.bedtime_bridge_awake_for_day`; optionally `switch.bedtime_bridge_sleep_in` and your notify action. One automation per person. | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2F2tk785fryr-tech%2Fbedtime-bridge-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fbedtime_bridge%2Fbedtime_bridge_snooze_fade.yaml) |
| **Wake sequence** | Your wake-up when you stop the alarm: day mode on, then the bedside lights (before sunrise and a while after) or lights off and sleep-in cleared in daylight. Skips the lights next to someone still asleep. | `switch.bedtime_bridge_stop` (and `switch.bedtime_bridge_early_wake_up` if you like), `switch.bedtime_bridge_awake_for_day`, your bedside lights; optionally the other side's lights and bed sensor, `switch.bedtime_bridge_sleep_in`, and anything else to do. | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2F2tk785fryr-tech%2Fbedtime-bridge-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fbedtime_bridge%2Fbedtime_bridge_wake_sequence.yaml) |
| **Bedtime routine** | A one-tap good night: every light off except your keep-on ones, a living area and an outdoor area only when nobody's using them, curtains closed. If it left something on, one push with a "Turn them off anyway" button and an optional announcement. | `switch.bedtime_bridge_bedtime`; your keep-on lights; the living area's occupancy sensors, media players and lights; the outdoor area's door sensor and lights; curtains; your notify action; text-to-speech and speakers if you want them. | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2F2tk785fryr-tech%2Fbedtime-bridge-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fbedtime_bridge%2Fbedtime_bridge_bedtime_routine.yaml) |
| **Exit latch** (for your own latch) | A left-the-bedroom latch with a return-to-bed reset: on when the bedroom door opens, off again once you've been back in bed a while (a night-time trip doesn't stand down the backstop). The integration's own `switch.bedtime_bridge_exit_latch` already follows your door sensor, so you only need this for your own logic. | Your bedroom door sensor, your bed sensor, and a toggle helper of your own (then pick that helper as **Use mine** for the latch under **Configure**). | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2F2tk785fryr-tech%2Fbedtime-bridge-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fbedtime_bridge%2Fbedtime_bridge_exit_latch.yaml) |

**The Missing plan blueprint's shift sensor** is a sensor of yours whose `next_shift_start`
attribute holds when your next shift starts (a timestamp), for example a template sensor that
reads your work calendar. It's the same sensor you pick as **Shift sensor** under **Configure →
Shifts and calendar**. Without one, skip that blueprint.

---

## 6. Check it works

- **Settings → Devices & services → Bedtime Bridge** shows its switches and sensors: next alarm,
  last plan, the backstop times, snoozing.
- In the app, **Settings → Home Assistant** (Smart home › Home Assistant when grouped) shows **Connected**.
- Tap **Set alarms** in the app: the integration's Last plan sensor updates. (**Preview
  tomorrow** only shows a plan and sets nothing, so the sensor stays as it was.)

---

## Troubleshooting

- **The app says "Not connected":** check Home Assistant is reachable from your phone (try the
  Companion app), then pair again with the link under **Configure**.
- **Alarms weren't set at bedtime:** the Missing plan blueprint warns you about this. Check the
  integration's Last plan sensor and the app's **Settings → The app → History** (**Advanced → History**
  when Settings is grouped).
- **A switch never turns on:** check the helper's choice under **Configure**. With **Use mine**
  and nothing picked, that helper isn't used. A binary sensor is only read.
- **The backstop never runs:** its status sensor (`sensor.bedtime_bridge_ladder_status`) must read
  `armed`; the sensor's reason says why it isn't.
- **After an update:** restart Home Assistant. Your settings and pairing stay.

### Upgrading to 0.4.12

Earlier versions filled in three backstop options for you when you left them empty. From
0.4.12, an empty option means **none**:

- **Restart automations:** none are restarted when a morning changes.
- **Old rung sensors:** the house backstop reads none of your own sensors.
- **Phone notifications to clear:** only the backstop blueprint's own notification is cleared.

If you relied on the old defaults, pick them in **Configure → Backstops** after updating (the
notification tags are on **Configure → Phone** from 0.4.12.5). If you
never used them (a new install, or the blueprints here), there's nothing to do.

### Moving from another repository

If you installed Bedtime Bridge from a different repository before, you don't need to set it up
again. The pairing, options, switches and history live in Home Assistant, not with the repository
the files came from, so only the files change. All before one restart:

1. **Marketplace → ⋮ → Custom repositories:** add
   `https://github.com/2tk785fryr-tech/bedtime-bridge-homeassistant` (Type: Integration).
2. Remove the old Bedtime Bridge entry from the Marketplace.
3. Download Bedtime Bridge from this repository straight away (removing the old entry may delete
   the files).
4. Restart Home Assistant. Bedtime Bridge comes back with its settings.

---

Questions or ideas: open an issue in this repo.
