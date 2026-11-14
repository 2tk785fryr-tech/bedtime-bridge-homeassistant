# Bedtime Bridge for Home Assistant

> 🌐 **[See the website](https://2tk785fryr-tech.github.io/bedtime-bridge-homeassistant/)**

**Up on time, every time.**

The Home Assistant side of the [Bedtime Bridge](https://github.com/2tk785fryr-tech/bedtime-bridge-showcase) iPhone app. The app reads your shifts and
appointments from your calendar and sets your wake-up alarms. This integration lets your house
take part: it sets the alarms at bedtime, runs your wake-up and bedtime routines when you snooze,
stop or go to bed, and steps in with lights and speakers if you're still in bed when you should
be up.

This repo holds only the latest version of the integration, plus generic blueprints you can
import with one tap.

---

## What you need

- Home Assistant 2025.1 or later.
- The Bedtime Bridge app on your iPhone, with **Home Assistant** chosen as your home in the app.
- The Home Assistant Companion app on the same iPhone (for notifications and your phone's
  location).

---

## 1. Install the integration

### From the Marketplace (recommended)

1. In Home Assistant, open **Marketplace** → **⋮** (top right) → **Custom repositories**.
2. **Repository:** `https://github.com/2tk785fryr-tech/bedtime-bridge-homeassistant`
   **Type:** Integration → **Add**.
3. Search for **Bedtime Bridge** → **Download**.
4. Restart Home Assistant.

> 📷 *Screenshot: the Custom repositories dialog*

Updates then show up in the Marketplace like any other.

### By hand

Copy `custom_components/bedtime_bridge` into your Home Assistant `config/custom_components/`
folder, then restart.

---

## 2. Add it and pair your phone

1. **Settings → Devices & services → Add integration → Bedtime Bridge.**
2. Pick **you** (your person or phone tracker) so it knows when you're home.
3. In the app: **Settings → Home → Home Assistant → Pair**, and follow the steps. The app and the
   integration exchange a code; nothing to type by hand.

> 📷 *Screenshot: the app's pairing screen*

The rest of the integration's options are all **optional**. Add the ones for the features you
want (the next section says which).

---

## 3. Helpers (the on/off switches your automations watch)

The integration turns these on and off so your own automations can react. Make the ones you want
in **Settings → Devices & services → Helpers → Create helper → Toggle**, then pick each one in the
integration's options (**Settings → Devices & services → Bedtime Bridge → Configure**).

| Helper (suggested name) | Type | Turned on / off when | Used for |
|---|---|---|---|
| **Bedtime Routine** | Toggle | On when you start your bedtime routine in the app | Your bedtime automation (lights down, lock up) |
| **Alarm Snoozed** | Toggle | On when you snooze | A gentle light ramp while you snooze |
| **Alarm Stopped** | Toggle | On when you stop the alarm | Your wake-up sequence (lights, curtains) |
| **Woke Before Alarm** | Toggle | On when you end the alarms before they ring | The same wake-up, started early |
| **Sleep In** | Toggle | On when your first alarm is after the curtains would open, or you skip a morning | Keep curtains and lights from waking you |
| **Awake For Day** | Toggle | On at Good Morning; back on if you undo bedtime | Your house's day mode |
| **Napping** | Toggle | On while a nap at home runs | Keep the house quiet |
| **Winding Down** | Toggle | On from wind-down until you go to bed | Warm, dim lights in the evening |
| **Left The Bedroom** | Toggle | On the first time the bedroom door opens each morning (set by the Bedroom Exit Latch blueprint) | Tells the integration you're really up |

> **Coming soon:** the integration will be able to make these itself (a switch for each), or use
> helpers you already have. For example, if you already have an "Asleep" helper, it can read that
> the other way round as Awake For Day.

Optional entities it can also use: a **bed sensor** (your side), your **work calendar**, a
**weather** entity (bad-weather extra time), and your phone's **battery** sensors (charge
warning).

---

## 4. Blueprints (tap to import)

Each blueprint is generic: nothing is filled in for you. Tap **Import**, then create an automation
from it and pick your own lights, speakers and sensors.

### Recommended

| Blueprint | What it does | Import |
|---|---|---|
| **Wake Deadline Backstop** | If you're still in bed as your get-up deadline nears and passes, it steps up: a lamp and a push, then a critical push, speakers and full lights, until you're up. Silent on every normal morning. Uses the integration's early, deadline and late times (set from your leave-by time). | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](#) |
| **Bedtime Bridge alarm event** | Runs your actions when the app reports an alarm event: snoozed, stopped, alarms planned or cancelled, leave-by updated. One automation per event. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](#) |
| **Bedroom Exit Latch** | Turns on Left The Bedroom the first time the bedroom door opens each morning, and resets it at night. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](#) |

### Optional

| Blueprint | What it does | Import |
|---|---|---|
| **Bedtime Bridge missing plan** | Warns you if a work day is coming but the app hasn't set tonight's alarms (the bedtime shortcut never ran, or calendar access was off). Checks once you've been in bed a while. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](#) |
| **Wake Snooze Fade** | Brings a light up slowly while you snooze. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](#) |
| **Wake Sequence** | Your wake-up: lights, curtains, a speaker, when you stop the alarm. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](#) |
| **Bedtime Routine** | Your bedtime: lights down, doors locked, when Bedtime Routine turns on. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](#) |
| **Bedtime Bridge nap** | When you start a nap in the app: closes the curtains and turns off lights in that room, then puts them back when the nap ends. Add the room's bed or presence sensors so a nap on the couch leaves the bedroom alone. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](#) |
| **Bedtime Bridge wind-down** | When wind-down starts: warm, dim lights (only the ones already on), curtains closed, anything else you add. Leaves the lights for your bedtime routine when it ends. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](#) |

When creating an automation from a blueprint, each field says what to pick (for example "your
bed sensor, your side"). Leave optional fields empty to skip that step.

---

## 5. Check it works

- **Settings → Devices & services → Bedtime Bridge** shows its sensors: next alarm, last plan,
  the backstop times, snoozing.
- In the app, **Settings → Home Assistant** shows **Connected**.
- Tap **Preview tomorrow** in the app: the integration's last plan sensor updates.

---

## Troubleshooting

- **The app says "Not connected":** check Home Assistant is reachable from your phone (try the
  Companion app), then pair again from the app.
- **Alarms weren't set at bedtime:** the missing plan blueprint warns you about this. Check the
  integration's **last plan** sensor and the app's **Settings → The app → History**.
- **A helper never turns on:** make sure it's picked in the integration's options.
- **After an update:** restart Home Assistant. Your settings and pairing stay.

### Moving from an older install

If you installed Bedtime Bridge from a different repository before, you don't need to set it up
again: your pairing and settings stay. In the Marketplace, add this repository (step 1), remove
the old Bedtime Bridge entry, download Bedtime Bridge from this one, then restart.

---

Questions or ideas: open an issue in this repo.
