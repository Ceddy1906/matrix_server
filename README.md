# Matrix Messenger for Home Assistant

Send messages from Home Assistant to [Matrix](https://matrix.org) rooms — including encrypted rooms (E2EE). Ask questions and react to text replies or emoji reactions in automations.

---

## Features

- **Send messages** to one or more Matrix rooms via service call or `notify.*` entity
- **Ask questions** — send a question to a room and wait for a reply (text or emoji reaction)
- **E2EE support** — end-to-end encrypted rooms via [matrix-nio](https://github.com/poljar/matrix-nio)
- **Two auth methods** — username + password, or access token
- **Fully GUI-configurable** — no YAML required, all settings via the HA config flow
- **HACS-installable**

---

## Requirements

| Requirement | Details |
|---|---|
| Home Assistant | ≥ 2026.4.0 |
| Python package | `matrix-nio[e2e] >= 0.21.0` (installed automatically) |
| Native library | `libolm` — pre-installed in HA OS, Supervised, and Container |

> **Note for HA Core (venv) installations:** `libolm` must be installed manually on the host system (`apt install libolm-dev` on Debian/Ubuntu).

---

## Installation

### Via HACS (recommended)

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**
2. Add your Gitea repository URL, category: **Integration**
3. Search for **Matrix Messenger** and install
4. Restart Home Assistant

### Manual

1. Copy the `custom_components/matrix_messenger/` folder into your HA config directory
2. Restart Home Assistant

---

## Configuration

Go to **Settings → Devices & Services → Add Integration** and search for **Matrix Messenger**.

### Step 1 — Matrix Server

| Field | Example |
|---|---|
| Homeserver URL | `https://matrix.org` |
| Authentication method | Username + Password *or* Access Token |

### Step 2a — Username + Password

| Field | Notes |
|---|---|
| Matrix User ID | `@youruser:matrix.org` |
| Password | Your Matrix account password |
| Device name | Shown in your Matrix client's session list (optional) |

### Step 2b — Access Token

Retrieve your token in your Matrix client: **Settings → Security → Sessions → Show access token**.

| Field | Notes |
|---|---|
| Matrix User ID | `@youruser:matrix.org` |
| Access Token | Paste the token here; the device ID is fetched automatically |

### Step 3 — Room Selection

All rooms the account has joined are listed. Select one or more rooms.

**Enable background sync** — when enabled, the integration polls Matrix every 5 seconds continuously. Required if you want to receive replies to questions without triggering `ask_question` first.

---

## Reconfiguring rooms

Go to **Settings → Devices & Services → Matrix Messenger → Configure** to change the selected rooms or toggle background sync at any time.

---

## Services / Actions

### `matrix_messenger.send_message`

Sends a plain-text message to a room.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `room_id` | string | ✓ | Matrix room ID, e.g. `!abc123:matrix.org` |
| `message` | string | ✓ | Message text |

**Example:**
```yaml
action: matrix_messenger.send_message
data:
  room_id: "!abc123:matrix.org"
  message: "Front door opened."
```

---

### `matrix_messenger.ask_question`

Sends a question to a room and waits for a reply. Once a matching reply arrives, the event `matrix_messenger_response` is fired. After the timeout (default 30 min) the question expires silently.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `room_id` | string | ✓ | — | Matrix room ID |
| `question` | string | ✓ | — | Question text |
| `options` | list of strings | | `[]` | If set, only these exact replies (or emoji reactions) are accepted |
| `timeout` | integer (seconds) | | `1800` | How long to wait. Min 60, max 7200 |

**Example — free-text reply:**
```yaml
action: matrix_messenger.ask_question
data:
  room_id: "!abc123:matrix.org"
  question: "Should I water the plants?"
```

**Example — constrained options:**
```yaml
action: matrix_messenger.ask_question
data:
  room_id: "!abc123:matrix.org"
  question: "Alarm triggered — is this a false alarm?"
  options:
    - "Yes"
    - "No"
  timeout: 300
```

The integration appends the options to the message:
> Alarm triggered — is this a false alarm?
>
> Mögliche Antworten: Yes / No

---

## Notify entities

Each configured room also creates a `notify.*` entity:

```
notify.matrix_<room display name>
```

These appear in the HA notification UI and can be used anywhere a notify target is accepted.

**Example:**
```yaml
action: notify.matrix_wohnzimmer
data:
  title: "Waschmaschine"
  message: "Programm beendet."
```

The title is prepended in bold: **Waschmaschine**\nProgramm beendet.

---

## Events

### `matrix_messenger_response`

Fired when a reply to an `ask_question` call is received.

| Attribute | Type | Description |
|---|---|---|
| `question_id` | string (UUID) | Unique ID of the question (from the service call) |
| `room_id` | string | Matrix room ID where the reply was received |
| `response` | string | The reply text or emoji |
| `response_type` | `"text"` or `"emoji"` | How the reply was sent |
| `sender` | string | Matrix user ID of the person who replied |

---

## Automation examples

### Send a notification when motion is detected

```yaml
automation:
  trigger:
    - platform: state
      entity_id: binary_sensor.front_door_motion
      to: "on"
  action:
    - action: matrix_messenger.send_message
      data:
        room_id: "!abc123:matrix.org"
        message: "Motion detected at the front door."
```

---

### Ask a question and branch based on the answer

```yaml
automation:
  alias: "Alarm confirmation"
  trigger:
    - platform: state
      entity_id: alarm_control_panel.home
      to: "triggered"
  action:
    # 1. Send the question — store question_id for matching
    - action: matrix_messenger.ask_question
      data:
        room_id: "!abc123:matrix.org"
        question: "Alarm triggered! False alarm?"
        options:
          - "Yes"
          - "No"
        timeout: 300
    # 2. Wait for the response event
    - wait_for_trigger:
        - platform: event
          event_type: matrix_messenger_response
          event_data:
            room_id: "!abc123:matrix.org"
      timeout: "00:05:00"
      continue_on_timeout: true
    # 3. Branch
    - choose:
        - conditions:
            - condition: template
              value_template: "{{ wait.trigger.event.data.response == 'Yes' }}"
          sequence:
            - action: alarm_control_panel.alarm_disarm
              target:
                entity_id: alarm_control_panel.home
        default:
          - action: notify.matrix_wohnzimmer
            data:
              message: "Alarm not confirmed — police notified."
```

---

### React to emoji reactions (👍 / 👎)

```yaml
automation:
  alias: "Heating approval"
  trigger:
    - platform: time
      at: "17:00:00"
  action:
    - action: matrix_messenger.ask_question
      data:
        room_id: "!abc123:matrix.org"
        question: "Turn on heating now?"
        options:
          - "👍"
          - "👎"
        timeout: 900
    - wait_for_trigger:
        - platform: event
          event_type: matrix_messenger_response
          event_data:
            room_id: "!abc123:matrix.org"
      timeout: "00:15:00"
      continue_on_timeout: true
    - if:
        - condition: template
          value_template: "{{ wait.trigger.event.data.response == '👍' }}"
      then:
        - action: climate.turn_on
          target:
            entity_id: climate.living_room
```

---

## E2EE key storage

The integration stores matrix-nio's E2EE keys (Olm/Megolm sessions) in:

```
<ha-config-dir>/.storage/matrix_messenger/
```

**Back this directory up** together with your HA config. If it is deleted, the integration will re-upload keys and may lose access to past encrypted messages.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ImportError: libolm` on startup | `libolm` not installed | `apt install libolm-dev` (HA Core only) |
| Rooms list is empty after login | Account not joined to any rooms | Join at least one room in your Matrix client first |
| Encrypted messages not decrypted | Keys missing (first sync) | Wait for the first full sync to complete after setup |
| `ask_question` never fires event | Sync not running | Enable *background sync* in the integration options, or check that no timeout expired |

---

## License

MIT
