# telethon

Streamlit + [Telethon](https://docs.telethon.dev/) outreach console with anti-ban safeguards: daily caps, random delays, dedup list, and automatic stop on flood errors.

**Repository:** [github.com/liam1217zy-cpu/telethon](https://github.com/liam1217zy-cpu/telethon)

## Requirements

- Python 3.10+
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
- A personal Telegram account (sender)

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

Open `http://localhost:8501`.

## Customer list format (CSV or Excel)

| Column | Purpose |
|--------|---------|
| `username` | Your internal reference (shown in logs only) |
| `name` | Greeting in the message (`Hi Mr/Ms {name}!`) |
| `phone` | Customer phone with country code — **used to send** |

Aliases accepted: `mobile`, `contact number`, `phone number`, etc.

## Sender vs customer phone

- **Form field “Your Telegram login phone”** — the account that sends messages.
- **List `phone` column** — each customer’s number (CSV, `.xlsx`, `.xls`, `.xlsm`).

## Anti-ban defaults

- 20 messages per sender account per day
- 5–10 minute random delay after each successful send
- `sent_list.txt` — each target is only attempted once
- Stops on `PeerFloodError` / `FloodWaitError`

## Files (local, not committed)

- `session_*.session` — Telegram login session
- `sent_list.txt` — dedup log
- `daily_send_counter.json` — daily send count

## License

Private use by the repo owner. Comply with [Telegram Terms of Service](https://telegram.org/tos).
