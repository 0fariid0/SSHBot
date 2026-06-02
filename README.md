# SSHBot Pro v4 - Real SSH Keychain + Multi Upload

این نسخه Keychain واقعی SSH دارد؛ یعنی می‌توانی مثل Termius یک SSH Private Key از نوع OpenSSH/RSA/ECDSA/ED25519 ذخیره کنی و بعد برای اتصال فقط IP و Port بدهی.

## نصب مستقیم از GitHub

فایل `install.sh` این نسخه را داخل ریپوی خودت جایگزین کن و بعد اجرا کن:

```bash
curl -fsSL https://raw.githubusercontent.com/0fariid0/SSHBot/main/install.sh | sudo bash
```

یا با توکن مستقیم:

```bash
curl -fsSL https://raw.githubusercontent.com/0fariid0/SSHBot/main/install.sh | sudo BOT_TOKEN='TELEGRAM_BOT_TOKEN' bash
```

## دستورهای مهم

- `/start` منوی اصلی
- `/version` نمایش نسخه نصب‌شده
- `/keychain` ذخیره یوزرنیم + SSH Private Key پیش‌فرض
- `/quickssh IP:PORT` اتصال با Keychain فقط با IP و Port
- `/servers` مدیریت سرورها
- `/serverkey NAME` ذخیره SSH Private Key برای یک سرور ذخیره‌شده
- `/serverpass NAME` ذخیره پسورد برای یک سرور ذخیره‌شده
- `/upload /remote/path` آپلود چند فایل روی سرور متصل
- `/done` پایان حالت آپلود

## روش استفاده از Keychain واقعی

1. داخل تلگرام بزن: `/keychain`
2. یوزرنیم SSH را بفرست؛ مثلاً `root`
3. Private Key را کامل بفرست، از `-----BEGIN ... PRIVATE KEY-----` تا `-----END ... PRIVATE KEY-----`
4. اگر کلید Passphrase دارد، Passphrase را بفرست؛ اگر ندارد فقط `.` بفرست.
5. برای اتصال بزن: `/quickssh 1.2.3.4:22`

## نکته امنیتی

Private Key و Passphrase داخل `/opt/sshbot/data/servers.json` رمزنگاری‌شده ذخیره می‌شوند. کلید رمزنگاری در این مسیر است:

```bash
/opt/sshbot/data/keychain.key
```

از این فایل بکاپ امن بگیر. اگر این فایل حذف شود، Keychain ذخیره‌شده قابل بازیابی نیست. اگر Private Key را جایی عمومی یا در اسکرین‌شات نشان دادی، حتماً کلید جدید بساز و public key قبلی را از سرورها حذف کن.
