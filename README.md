# XVIP Hybrid Telegram Bot

Ek automated pipeline jo source channels monitor karta hai, posts ko converter bots ke through process karta hai, aur final output destination channels pe bhejta hai.

---

## Architecture

```
Source Channel (Tera)  →  Tera Converter Bot  →  Tera Destination Channel
Source Channel (Disk)  →  Disk Converter Bot  →  Disk Destination Channel
```

**Do components hain:**

- **Admin Bot** — Telegram bot token se chalta hai. Sirf admin commands handle karta hai (`/start`, `/status`, `/login`)
- **Userbot** — Aapke personal Telegram account ka StringSession use karta hai. Source channels monitor karta hai aur converter bots se reply receive karta hai

**Database:** Supabase — sirf StringSession store karta hai

**Deploy:** Railway (ya koi bhi platform jo env vars support kare)

---

## Pipelines

### Tera Pipeline
- Source channel post me `tera` word (URL me) hona chahiye
- Post `TERA_CONVERTER_BOT` ko bheja jaata hai
- Converter ka reply check hota hai:
  - Media (photo/video) hona chahiye
  - Reply ke kisi URL me `tera` word hona chahiye
- Condition pass hone pe `TERA_DESTINATION` pe bheja jaata hai

### Disk Pipeline
- Bilkul same logic — sirf `disk` keyword aur alag converter/destination

---

## Environment Variables

| Variable | Description | Example |
|---|---|---|
| `API_ID` | Telegram App ID (my.telegram.org) | `12345678` |
| `API_HASH` | Telegram App Hash (my.telegram.org) | `abcdef1234567890abcdef` |
| `BOT_TOKEN` | Admin bot token (@BotFather se) | `123456:ABC-DEF...` |
| `ADMIN_IDS` | Comma-separated Telegram user IDs jo bot use kar sakte hain | `987654321,123456789` |
| `SUPABASE_URL` | Supabase project URL | `https://xyz.supabase.co` |
| `SUPABASE_KEY` | Supabase anon/service key | `eyJhbGci...` |
| `TERA_SOURCE_CHANNELS` | Comma-separated channel IDs (Tera pipeline) | `-1001234567890,-1009876543210` |
| `DISK_SOURCE_CHANNELS` | Comma-separated channel IDs (Disk pipeline) | `-1001111111111` |
| `TERA_CONVERTER_BOT` | Tera converter bot username | `terabox_converter_bot` |
| `DISK_CONVERTER_BOT` | Disk converter bot username | `disk_converter_bot` |
| `TERA_DESTINATION` | Tera output channel username | `my_tera_channel` |
| `DISK_DESTINATION` | Disk output channel username | `my_disk_channel` |
| `STRING_SESSION` | (Optional) Telegram StringSession directly as env var — agar set hai toh Supabase skip hota hai | `1BVtsOK8Bu...` |

> **Note:** Channel IDs negative hote hain aur `-100` se start karte hain (supergroups/channels). `@` ke bina username dena hai converter/destination ke liye.

---

## Supabase Setup

1. [supabase.com](https://supabase.com) pe project banao
2. SQL Editor me yeh query chalao:

```sql
create table bot_config (
  key_name  text primary key,
  key_value text not null
);
```

3. Project Settings → API se `URL` aur `anon key` copy karo

---

## Local Machine pe Session Generate Karna

Railway ya kisi bhi server pe deploy karne se pehle **ek baar local machine pe session generate karna zaroori hai** — kyunki Telegram new login pe confirmation maangta hai jo sirf aapke phone pe aata hai.

### Step 1 — Dependencies install karo

```bash
pip install telethon
```

### Step 2 — Yeh script chalao

```python
# generate_session.py
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID   = int(input("API_ID: "))
API_HASH = input("API_HASH: ")

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print("\n✅ Session String (copy kar lo):\n")
    print(client.session.save())
```

```bash
python generate_session.py
```

- Phone number maangega (`+91XXXXXXXXXX` format)
- OTP aapke Telegram app pe aayega
- 2FA password maangega (agar enabled hai)
- **Terminal me ek lamba string print hoga — yahi StringSession hai**

### Step 3 — Session set karo

Session string teen tarike se set kar sakte ho (koi ek choose karo):

**Option A — `STRING_SESSION` env var (recommended, sabse simple)**

Railway/server ke environment variables me seedha daalo:
```
STRING_SESSION = 1BVtsOK8Bu4tGazk... (poora string)
```
Agar yeh set hai toh Supabase bilkul zaroorat nahi.

**Option B — Supabase Table Editor se manually**

Supabase Table Editor me `bot_config` table me row daalo:

| key_name | key_value |
|---|---|
| `telegram_string_session` | `(aapka session string)` |

**Option C — Bot ka `/login` command**

Bot deploy hone ke baad `/login` se authenticate karo — session automatically Supabase me save ho jaata hai.

---

## Deploy on Railway

### Step 1 — Repository

```bash
git init
git add main.py requirements.txt
git commit -m "init"
```

GitHub pe push karo aur Railway me connect karo.

### Step 2 — Environment Variables

Railway dashboard → project → Variables me sab env vars add karo (table upar dekho).

### Step 3 — Deploy

Railway automatically deploy kar dega. Pehli baar deploy hone ke baad:

1. Apne Telegram pe Admin Bot ko message karo
2. `/login` command bhejo
3. Phone number, OTP, aur 2FA (agar hai) daro
4. ✅ Userbot live ho jaayega — **reboot nahi chahiye**

---

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Bot ka status aur available commands dikhata hai |
| `/status` | Userbot connected hai ya nahi, aur kis account se |
| `/login` | Userbot authenticate karo (3-step: phone → OTP → 2FA) |

> Sirf `ADMIN_IDS` me listed users hi commands use kar sakte hain.

---

## Login Flow (Detail)

```
Admin: /login
Bot:   Phone number bhejo (+91XXXXXXXXXX)

Admin: +91XXXXXXXXXX
Bot:   OTP bheja gaya — turant bhejo (60 sec valid)

Admin: 12345
Bot:   (agar 2FA hai) → 2FA password maango
       (agar 2FA nahi) → ✅ Activated!

Admin: mypassword  (sirf 2FA case me)
Bot:   ✅ Userbot Activated! Logged in as: @username
```

Session automatically Supabase me save ho jaata hai. Restart pe session wahan se load hota hai — dobara login nahi karna.

---

## Project Structure

```
.
├── main.py           # Poora bot (single file)
├── requirements.txt  # Python dependencies
└── README.md         # Yeh file
```

---

## requirements.txt

```
telethon>=1.34.0
httpx>=0.27.0
cryptg          # optional — Telethon crypto speed up karta hai
```

---

## Troubleshooting

**Bot respond nahi kar raha**
- `ADMIN_IDS` check karo — aapka Telegram user ID wahan hona chahiye
- Railway logs dekho

**Userbot source channel monitor nahi kar raha**
- `/status` se confirm karo ki userbot connected hai
- `TERA_SOURCE_CHANNELS` / `DISK_SOURCE_CHANNELS` me IDs correct hain — `-100` prefix ke saath

**Channel ID kaise pata kare**
- Channel ko `@username_to_id_bot` ya similar bot se check karo
- Ya Telegram Web pe channel open karo, URL me number hota hai (uske aage `-100` lagao)

**Session expire ho gayi**
- `/login` se dobara authenticate karo
- Naya session Supabase me overwrite ho jaayega

**Converter reply destination pe nahi ja raha**
- Railway logs me dekho kya print ho raha hai
- Reply me media hai ya nahi check karo
- Reply ke URL me `tera`/`disk` word hai ya nahi

---

## Security

- Session string **private rakho** — iska matlab aapka poora Telegram account access hai
- `ADMIN_IDS` me sirf trusted users dalo
- Supabase key ko public repo me commit mat karo — `.env` file use karo locally
