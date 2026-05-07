# proton-telegram-bot

Telegram bot yang menerusin email dari **Proton Mail** ke chat Telegram
kamu — lewat Proton Mail Bridge — sambil ngatur banyak alias supaya
kamu bisa kasih alamat email beda ke setiap orang/layanan tanpa
pernah buka inbox Proton lagi.

> Ringkasnya: kamu kasih `vielz123@proton.me` ke website A, dan
> `vielz124@proton.me` ke website B. Bot watch keduanya. Setiap email
> masuk diteruskan ke chat Telegram dengan tombol "✓ Tandai sudah
> dibaca". Kalau alias kena spam, tinggal hapus aliasnya — alamat
> yang lain tetap aman.

---

## Untuk apa bot ini?

- **Privasi per-layanan.** Setiap akun online punya alias sendiri,
  kalau ada yang bocor / spam tinggal di-hapus tanpa ganggu yang lain.
- **Tidak perlu buka inbox Proton.** Verifikasi OTP / link reset
  password / notifikasi langsung muncul di Telegram dengan satu tap
  copy.
- **Lock-mode untuk pendaftaran.** Saat lagi daftar di satu layanan,
  kunci alias aktif lewat `/list` → bot cuma forward email yang
  dikirim ke alamat itu, jadi pesan dari alias lain tidak ngeganggu.
- **Recovery saat network blip.** Kalau VPS lagi flaky dan email
  tidak terforward, tombol `📂 Inbox` (atau `/inbox`) re-fetch 5 email
  terakhir langsung dari Proton.
- **Multi-akun, multi-user.** Satu deployment bisa jalan untuk
  beberapa user Telegram, masing-masing dengan akun Proton sendiri.

---

## Fitur utama

| Area | Apa yang bisa kamu lakukan |
|---|---|
| **Forward email** | Setiap email yang masuk ke alias diteruskan ke Telegram dengan subject + sender + body preview + tombol "✓ Tandai sudah dibaca" yang langsung label di Proton. |
| **Manage alias** | `/list` (drill-down per primary), `/addalias`, `/removealias`, `/history`, `/reset`, `/aliasinfo`. Kamu juga bisa `/sync` untuk import semua alamat dari akun Proton sekali tap. |
| **Bulk generate** | `/genaddr vielz 10` → bikin `vielz001…vielz010` lewat headless Chromium yang drive UI Proton. Resume otomatis kalau ada CAPTCHA. |
| **Lock alias aktif** | Tap alias di `/list` untuk lock — bot cuma forward email ke alamat itu sampai kamu `/unlock` atau pilih alias lain. Otomatis cocok untuk flow signup. |
| **Per-service labels** | Saat email pertama masuk untuk satu domain, bot tawarkan tombol untuk pasang label (mis. `Devin`, `GitHub`, `Skip — work`). Email berikutnya dari domain itu otomatis dapat label di Proton + di chat. |
| **Recovery** | `📂 Inbox` button + `/inbox [n]` re-fetch N email terakhir untuk alias aktif kalau listener kelewatan satu (mis. saat network blip). |
| **Restart instan** | `/resetbot` exit ke supervisor (systemd / pm2) → restart 5 detik tanpa SSH ke VPS. |
| **Health check** | `/cekimap` ping IMAP listener. Tombol "🩺 Cek IMAP listener (background)" dari `/list` jalanin probe penuh. |
| **Cleanup** | `/cleanmail` hapus history email lama dari chat (Telegram retention). `/disconnect` hapus akun + stop listener. |
| **Auto-sync** | Setiap N menit (default 5) bot scan inbox Proton untuk alias baru yang dibikin manual lewat web Proton, tambahkan ke daftar. |
| **Tahan blip jaringan** | HTTP timeouts 15–60 s + exponential backoff retry (1/3/7/15 s) untuk error transient `TimedOut` / `NetworkError` / `httpx.TimeoutException`. Email forward tidak hilang lagi karena network blip. |
| **Hardened systemd** | Unit file restart selamanya (`StartLimitIntervalSec=0`), survive OOM kill, priority CPU/IO tinggi. Bot praktis selalu jalan. |
| **Multi-user** | Setiap user Telegram punya database row terpisah; credentials Bridge di-encrypt at-rest dengan Fernet master key. |

---

## Requirements

- **Python ≥ 3.11**
- **[Proton Mail Bridge][bridge]** running di mesin yang sama dengan
  bot (atau accessible via SSH tunnel / WireGuard). Bridge listen di
  `127.0.0.1:1143` (IMAP) by default.
- **Telegram bot token** dari [@BotFather](https://t.me/botfather).
- (Opsional) Plan Proton yang allow banyak alias kalau mau pakai
  `/genaddr` / `/sync` (Mail Plus / Unlimited / Business).

[bridge]: https://proton.me/mail/bridge

> ⚠️ **Multi-user heads up.** Bridge cuma expose IMAP di mesin tempat
> dia jalan. Kalau mau host satu bot untuk banyak user, masing-masing
> user harus jalanin Bridge sendiri di mesin yang sama dengan bot,
> ATAU expose port IMAP-nya ke bot via private channel (SSH tunnel,
> WireGuard). **Jangan pernah expose Bridge IMAP ke internet
> publik.**

---

## Quick start (lokal, 5 menit)

```bash
git clone https://github.com/alviarts/proton-telegram-bot.git
cd proton-telegram-bot

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Generate Fernet key sekali, copy ke .env:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

cp .env.example .env
# edit .env, isi minimal:
#   TELEGRAM_BOT_TOKEN=<dari @BotFather>
#   ENCRYPTION_KEY=<key dari command di atas>
#   ALLOWED_USER_IDS=<your-telegram-user-id>     # WAJIB untuk private bot

python -m proton_telegram_bot
```

Cek user ID Telegram kamu lewat [@userinfobot](https://t.me/userinfobot).

---

## Deploy ke VPS

### Opsi A — Docker (paling simple)

```bash
git clone https://github.com/alviarts/proton-telegram-bot.git
cd proton-telegram-bot
cp .env.example .env  # edit isi token + key
docker compose up -d
docker compose logs -f bot
```

`docker-compose.yml` pakai `network_mode: host` jadi bot bisa reach
Bridge di `127.0.0.1:1143` tanpa konfig tambahan.

### Opsi B — systemd (high-availability, recommended buat VPS)

```bash
# 1) Clone & install
sudo git clone https://github.com/alviarts/proton-telegram-bot.git /opt/proton-telegram-bot
cd /opt/proton-telegram-bot
sudo python3 -m venv .venv
sudo .venv/bin/pip install .

# 2) Generate Fernet key & isi .env
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
sudo cp .env.example .env
sudo nano .env

# 3) Pasang unit file (sudah di-harden untuk auto-restart selamanya)
sudo cp deploy/proton-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now proton-telegram-bot

# 4) Verifikasi
sudo systemctl status proton-telegram-bot
sudo journalctl -u proton-telegram-bot -f
```

Unit file di `deploy/proton-telegram-bot.service` sudah di-set:

- `Restart=always` + `StartLimitIntervalSec=0` → systemd retry restart
  selamanya, tidak akan pernah nyerah.
- `RestartForceExitStatus` covers semua exit code + `SIGTERM`, jadi
  `os._exit(0)` dari `/resetbot` cleanly cycling process.
- `OOMPolicy=continue` + `OOMScoreAdjust=-200` → bot survive walaupun
  OOM killer reaping process lain.
- `Nice=-5`, `IOSchedulingPriority=2` → priority CPU/IO bumped supaya
  bot tetap responsive di VPS yang busy.
- `LimitNOFILE=65536` → tidak akan kena fd exhaustion saat banyak
  IMAP listener + httpx connection nyala.
- `WatchdogSec=15s` → kalau event-loop hang, systemd kill & restart
  otomatis.

Verifikasi hardening live:

```bash
systemctl show proton-telegram-bot \
  -p Restart -p StartLimitIntervalUSec -p OOMPolicy \
  -p OOMScoreAdjust -p Nice -p LimitNOFILE
```

### Update bot di VPS

```bash
cd /opt/proton-telegram-bot
sudo git pull
sudo .venv/bin/pip install .
sudo cp deploy/proton-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart proton-telegram-bot
```

---

## Setup Proton Bridge

Bot connect ke Bridge via IMAP. Pastikan Bridge running:

1. Install [Proton Mail Bridge][bridge] di mesin yang sama dengan bot.
2. Login dengan akun Proton kamu.
3. Di Bridge, masuk akun → catat:
   - **IMAP host**: biasanya `127.0.0.1`
   - **IMAP port**: biasanya `1143`
   - **Username**: alamat email Proton kamu
   - **Password**: password Bridge yang di-generate (bukan password Proton master)
4. Pakai credentials ini saat kamu `/connect` di chat Telegram.

> **Headless server?** Bridge punya CLI mode: `protonmail-bridge --cli`.
> Lihat [dokumentasi Bridge](https://proton.me/support/bridge).

### Opsional: auto-register akun (bot drives Bridge sendiri)

Set `BRIDGE_ADMIN_ENABLED=true` kalau bot jalan di host yang sama
dengan Bridge dan kamu mau `/connect` minta password **akun Proton**
(bukan password Bridge IMAP yang random). Bot bakal:

1. Stop `protonmail-bridge.service`.
2. Drive `bridge --cli login` pakai email + Proton password yang kamu
   ketik di `/connect`.
3. Kalau Proton minta CAPTCHA, bot forward URL-nya ke Telegram —
   solve di browser, balas `ok`, bot resume.
4. Restart `protonmail-bridge.service` & decrypt vault untuk ambil
   password IMAP yang Bridge baru bikin.
5. Save IMAP password ke DB bot (Fernet-encrypted) dan start listener.

Requirement extra: bot run as root (atau `BRIDGE_SUDO=true` + sudoers
rule), `cryptography` + `msgpack` + `pexpect` installed (`pip install
-e '.[bridge-admin]'`).

---

## Cara pakai harian

### 1. Setup pertama kali

```
/start                       # daftarkan chat
/connect                     # masuk dialog input Bridge IMAP credentials
/sync vielz <password>       # auto-import semua alamat dari akun Proton kamu
                             # (atau /addalias addr1@proton.me addr2@... manual)
```

### 2. Pakai alias untuk daftar di layanan baru

```
/list                        # lihat alias yang available, tap salah satu
                             # → bot kunci alias itu, kasih kamu tombol Copy
                             # → kamu paste alias di form signup di website
                             # → email verifikasi muncul di Telegram dalam ~5 detik
                             # → tap "✓ Tandai sudah dibaca" supaya Proton ditandai read
```

Setelah alias dipakai, statusnya berpindah ke `/history` dan tidak
muncul lagi di `/list` (kecuali kamu `/reset alias@proton.me`).

### 3. Pesan alias-aktif: 3 tombol cepat

Setelah lock alias, bot kirim pesan dengan 3 baris tombol:

- **📋 Copy email aktif** — bot kirim alias di pesan baru sebagai
  `<code>` tap-to-copy (untuk desktop yang tidak punya tap-to-copy
  di body HTML).
- **📥 Cek email sekarang** — paksa listener polling sekarang
  (tidak nunggu 5 detik berikutnya).
- **📂 Inbox** — re-fetch 5 email terakhir untuk alias aktif
  langsung dari Proton (recovery kalau listener kelewatan / VPS
  blip jaringan).

### 4. Recovery saat ada masalah

| Gejala | Solusi |
|---|---|
| Email udah masuk Proton tapi belum sampai Telegram | Tap **📂 Inbox** atau kirim `/inbox` |
| Listener stuck tidak polling | Kirim `/cekimap` (light probe) atau tap "🩺 Cek IMAP listener" di `/list` |
| Bot lambat respon / hang | Kirim `/resetbot` → systemd restart bot dalam 5 detik |
| Lupa alias mana yang aktif | Lihat header `/start` atau `/list` (tampil "🔒 Aktif: …") |
| Mau pakai alias lain sementara | `/unlock` → balik ke mode forward semua alias |

---

## Daftar lengkap commands

| Command | Fungsi |
|---|---|
| `/start` | Daftarkan chat & tampilan panduan singkat. |
| `/connect` | Dialog input Bridge IMAP credentials. |
| `/disconnect` | Hapus credentials + stop listener untuk akun yang dipilih. |
| `/list` | Daftar alias available sebagai inline buttons (tap untuk lock). |
| `/accounts` | Shortcut untuk `/list`. |
| `/unlock` | Lepas kunci alias aktif. |
| `/inbox [n]` | Re-fetch N email terakhir untuk alias aktif (default 5, max 20). Recovery kalau listener kelewatan. |
| `/history` | Daftar alias yang sudah dipakai. |
| `/reset a@b.com` | Pindahkan alias kembali ke "available". |
| `/addalias a@b.com c@d.com …` | Tambah alias manual (banyak sekaligus juga bisa). |
| `/removealias a@b.com` | Hapus alias dari daftar (juga di Proton kalau mau). |
| `/aliasinfo a@b.com` | Detail per-alias (kapan dibuat, owner primary, status). |
| `/sync user password` | Auto-sync semua alamat dari akun Proton via API Bridge. |
| `/setprotonpw` | Simpan password master Proton (untuk `/genaddr`). |
| `/genaddr <base> <count> [@domain]` | Bulk-create alamat lewat headless Chrome (mis. `/genaddr vielz 50`). |
| `/services` | Daftar service-label yang sudah kamu set. |
| `/cekimap` | Ping IMAP listener (light probe). |
| `/cleanmail` | Sweep & hapus pesan email lama dari chat (Telegram retention). |
| `/resetbot` | Force restart bot lewat supervisor (systemd / pm2). |
| `/cancel` | Batalkan dialog conversation yang lagi jalan. |

---

## Environment variables

Minimal config (`.env`):

```bash
TELEGRAM_BOT_TOKEN=123456:ABC...      # dari @BotFather
ENCRYPTION_KEY=<fernet-key>           # generate sekali, jangan ganti
ALLOWED_USER_IDS=123456789            # comma-separated, restrict siapa yang bisa pakai
LOG_LEVEL=INFO                        # DEBUG/INFO/WARNING/ERROR
```

Reference lengkap:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Token dari @BotFather. |
| `ENCRYPTION_KEY` | yes | URL-safe base64 32-byte Fernet key untuk encrypt password Bridge & Proton di DB. |
| `DATABASE_PATH` | no | Path SQLite. Default: `data/bot.sqlite3`. |
| `ALLOWED_USER_IDS` | no | List user ID Telegram yang boleh pakai bot. Kosong = siapa saja (NOT recommended). |
| `LOG_LEVEL` | no | `DEBUG`/`INFO`/`WARNING`/`ERROR`. Default: `INFO`. |
| `ALIAS_SYNC_INTERVAL_MINUTES` | no | Interval auto-sync inbox cari alias baru. Default: 5 menit. |
| `PROTON_BOT_VERBOSE_THIRDPARTY` | no | `1` = lepas mute log `aioimaplib`/`httpx`/`httpcore` saat `LOG_LEVEL=DEBUG`. Default: muted (untuk privacy — `aioimaplib` DEBUG bisa dump body email + OTP ke journalctl). |
| `BRIDGE_ADMIN_ENABLED` | no | `true` = `/connect` minta password Proton & auto-register akun di Bridge. Default: `false`. |
| `BRIDGE_ADD_ACCOUNT_SCRIPT` | no | Path ke `bridge_add_account.py`. |
| `BRIDGE_DECRYPT_VAULT_SCRIPT` | no | Path ke `bridge_decrypt_vault.py`. |
| `BRIDGE_VAULT_PATH` | no | File vault Bridge. Default: `/root/.config/protonmail/bridge-v3/vault.enc`. |
| `BRIDGE_VAULT_KEY_COMMAND` | no | Shell command yang print raw vault key ke stdout. |
| `BRIDGE_CAPTCHA_URL_FILE` | no | Path file untuk write CAPTCHA URL. Default: `/tmp/bridge_captcha_url.txt`. |
| `BRIDGE_CAPTCHA_DONE_FLAG` | no | Path flag yang dibikin bot saat CAPTCHA solved. Default: `/tmp/bridge_captcha_done.flag`. |
| `BRIDGE_CAPTCHA_TIMEOUT_SECONDS` | no | Timeout nunggu CAPTCHA solved. Default: `600`. |
| `BRIDGE_PYTHON` | no | Python interpreter untuk helper scripts. Default: `python3`. |
| `BRIDGE_SUDO` | no | `true` = wrap helper invocation pakai `sudo -n …`. Default: `false`. |

---

## Troubleshooting

**Bot tidak respon `/start`:**
```bash
sudo systemctl status proton-telegram-bot
sudo journalctl -u proton-telegram-bot -n 100 --no-pager
```
Kalau status `failed`, biasanya `.env` belum lengkap atau Bridge
belum jalan. Hardened unit akan auto-restart selamanya jadi cek
journal untuk error sebenarnya.

**Email Proton masuk tapi tidak ada di Telegram:**
1. Tap **📥 Cek email sekarang** atau **📂 Inbox** di pesan alias-aktif.
2. Kalau masih tidak datang, kirim `/cekimap` untuk cek IMAP listener.
3. Restart bot dengan `/resetbot`.

**Bot crash setiap beberapa menit dengan `httpcore.ConnectTimeout`:**
Update ke versi terbaru — versi v2026.05+ punya retry helper +
HTTP timeout yang sudah generous (15–60 s). Lihat `journalctl` untuk
WARNING `transient network error in handler:` (artinya retry helper
lagi jalan, bukan bug).

**`LOG_LEVEL=DEBUG` bikin journal banjir IMAP frame raw:**
Default behavior sudah mute `aioimaplib`/`httpx`/`httpcore` ke
WARNING walau root level DEBUG. Set
`PROTON_BOT_VERBOSE_THIRDPARTY=1` di `.env` kalau memang butuh
DEBUG dari library itu.

---

## Development

```bash
pip install -e ".[dev]"
ruff check .
pytest
```

Tech stack:
- `python-telegram-bot ≥ 21.6` (async, jobqueue, conversation)
- `aioimaplib` (IMAP listen Bridge)
- `aiosqlite` (SQLite single-file persistence per-user)
- `cryptography.Fernet` (encrypt creds at rest)
- `pydantic-settings` (config dari `.env`)
- `playwright` (untuk `/genaddr` headless browser flow)

Test suite ada 358+ tests covering: handlers, IMAP listener, manager
state, retry helper, on_error, ApplicationBuilder timeout, log muting,
inbox button placement, slash menu, dst.

---

## Security notes

- **Bridge passwords** di-encrypt at rest dengan `cryptography.Fernet`.
  Hilang `ENCRYPTION_KEY` = harus `/connect` ulang.
- **Proton master password** dari `/setprotonpw` di-encrypt cara yang
  sama tapi tidak bisa di-revoke per-device — siapa pun yang punya
  akses ke `.env` (dengan `ENCRYPTION_KEY`) **dan** SQLite DB bisa
  full sign-in ke Proton kamu. Cuma enable di host yang kamu treat
  sebagai authoritative; rotate password Proton kalau host pernah
  compromised.
- **Body email** di Telegram bisa berisi konten sensitif. Treat chat
  history sama dengan inbox.
- **JANGAN** commit `.env` atau SQLite DB. Keduanya excluded di
  `.gitignore`.
- Set `ALLOWED_USER_IDS` ke user ID Telegram kamu saja. Tanpa itu,
  siapa pun yang nemu bot kamu bisa pakai.
- Default mute third-party DEBUG logs supaya `aioimaplib` tidak dump
  raw IMAP frame (body email, OTP, link reset password) ke journalctl
  walau `LOG_LEVEL=DEBUG`.

---

## License

MIT. Lihat [LICENSE](LICENSE).
