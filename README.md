# SSHBot Pro v3

این نسخه داخل `install.sh` خود فایل اصلی ربات را هم دارد؛ بنابراین اگر روی GitHub فقط `install.sh` را جایگزین کنید، نسخه Pro واقعاً deploy می‌شود و دیگر به `ssh-bot.py` قدیمی ریپو وابسته نیست.

## نصب از GitHub

1. فایل `install.sh` همین نسخه را در ریپوی خودتان جایگزین کنید.
2. اجرا کنید:

```bash
curl -fsSL https://raw.githubusercontent.com/0fariid0/SSHBot/main/install.sh | sudo bash
```

یا با توکن مستقیم:

```bash
curl -fsSL https://raw.githubusercontent.com/0fariid0/SSHBot/main/install.sh | sudo BOT_TOKEN='YOUR_BOT_TOKEN' bash
```

## بررسی نصب درست

روی سرور:

```bash
grep BOT_VERSION /opt/sshbot/ssh-bot.py
systemctl status sshbot --no-pager
journalctl -u sshbot -n 80 --no-pager
```

داخل تلگرام:

```text
/version
```

باید این را نشان دهد:

```text
SSHBot Pro v3.0.0 - Keychain + Multi Upload
```

## امکانات جدید

- `/keychain` ذخیره یوزرنیم/پسورد پیش‌فرض
- `/quickssh host[:port]` اتصال فقط با IP و پورت
- `/upload [remote_dir]` آپلود چند فایل روی سرور فعال
- `/done` پایان حالت آپلود
- `/version` نمایش نسخه نصب‌شده
