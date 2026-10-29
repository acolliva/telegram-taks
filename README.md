# Task bot

A Telegram bot for a shared to-do list, made for a household. Everyone talks to the bot in their own private chat. There's no group, and the bot never writes first.

- **Adding** a task is just sending a message: text, a photo, or an Italian voice note.
- **Seeing** happens only when you ask, through two buttons that stay under the keyboard: **📋 Da fare** and **✅ Fatte**.
- **Marking done** is one tap. The bot records who did it and when, and you can undo.

It runs on a Raspberry Pi with long polling: no public IP, open ports or domain needed.

## Using it (in Italian, as the bot)

| You send | What happens |
|---|---|
| `Cambiare lampadina bagno` | new task |
| `Chiamare idraulico` + new line + `il numero è sul frigo` | first line is the title, the rest are details |
| `- tende` / `- mensola` / `- bolletta` (one per line) | a bulleted list becomes one task per line |
| a photo with a caption | task with that photo (an album gives one task with all the photos) |
| a photo without a caption, within 3 minutes of adding a task | attached to that task (with ↩️ to undo) |
| a voice note | transcribed into a task; the audio is kept and can be played back |

Each addition gets a short confirmation with **↩️ Annulla** and **➕ Dettagli**. Details mode lets you add text, photos or voice to the task until you tap ✔️ Fine.

**📋 Da fare** shows the open tasks, oldest first. Next to each task are its icons (📷 photo, 🎙 voice, 📝 notes) and how long it has been waiting.
- Tap a task to open its details: notes, who added it and when, 📷 photos (browsed in a single message, with ✖ to close), 🔊 audio, ➕ add details, 🗑 delete.
- Tap ☑️ to mark it done. **↩️ Annulla «…»** stays in the list for 15 minutes in case of a mistake.

**✅ Fatte** shows what was done, grouped by day, with time and person, plus how many tasks are still open. Buttons switch between today, 7 days and 30 days, and `/fatte 90` shows any number of days.

The chat stays tidy: tapping a menu button removes your message and the previous list, so there's always one list at the bottom. If someone adds or completes a task while your list is open, the list updates silently, without any notification.

Commands: `/dafare`, `/fatte [days]`, `/aiuto`.

## Setup

### 1. Create the bot
Send `/newbot` to **@BotFather** and copy the token.

### 2. On the Raspberry
Python ≥ 3.11 is required, which comes with Raspberry Pi OS Bookworm.

```bash
git clone https://github.com/acolliva/telegram-taks.git ~/task-reminder-telegram
cd ~/task-reminder-telegram
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env    # BOT_TOKEN
.venv/bin/python -m taskbot
```

Then each person writes anything to the bot. People who aren't allowed yet get a reply with their id. Put the ids in `ALLOWED_USERS` (comma separated) and restart.

### 3. Voice notes
Choose one option in `.env`:

- **`TRANSCRIBER=local`** runs Whisper on the Pi. It's free and nothing leaves the house.
  ```bash
  .venv/bin/pip install -r requirements-voice.txt
  ```
  The model downloads from Hugging Face on first start (~500 MB for `small`). On a Pi 4, a 10-second note takes roughly 10–30 s with `small`, or a few seconds with `base`, which is less accurate. The bot shows "🎙️ Trascrivo…" meanwhile, and other people aren't blocked.
- **`TRANSCRIBER=openai`** with `OPENAI_API_KEY` is fast and very accurate in Italian, but paid per use.
- **`TRANSCRIBER=off`** saves voice notes as tasks titled "🎙️ Nota vocale" with the audio attached.

If transcription fails for any reason, the voice note is still saved with its audio.

### 4. Start automatically

```bash
sudo cp deploy/taskbot.service /etc/systemd/system/
sudo nano /etc/systemd/system/taskbot.service   # User and paths: match the folder name exactly
sudo systemctl daemon-reload
sudo systemctl enable --now taskbot
journalctl -u taskbot -f
```

**Updating:** `cd ~/task-reminder-telegram && git pull && sudo systemctl restart taskbot`

**Backup:** `sqlite3 tasks.sqlite3 ".backup backup.sqlite3"`

## Development

```bash
pip install -e ".[dev]"
pytest
```

`tests/test_flow.py` drives the whole bot against a fake Telegram API, covering adding, lists, done/undo, photos, albums, voice, details, deletion, paging and permissions. It needs no token or network.

```
taskbot/
  text.py        message → tasks, Italian date formatting
  db.py          SQLite: tasks, photos, open views
  render.py      texts and buttons
  transcribe.py  voice → text (faster-whisper / OpenAI / off)
  bot.py         Telegram handlers
```
