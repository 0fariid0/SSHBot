# SSHBot Pro

ربات تلگرام برای اتصال تعاملی SSH به سرور، با ترمینال زنده، کلیدهای کنترلی، مدیریت چند سرور، Keychain داخلی و آپلود چند فایل روی سرور.

## امکانات جدید این نسخه

- اتصال SSH تعاملی داخل تلگرام
- مدیریت چند سرور با دکمه‌های شیشه‌ای
- Keychain داخلی و رمزنگاری‌شده برای ذخیره پسورد SSH
- اتصال سریع با Keychain: فقط IP/Host و Port را می‌فرستی
- آپلود چند فایل از تلگرام به سرور از طریق SFTP
- پشتیبانی از انتخاب همزمان چند فایل در تلگرام؛ هر فایل جداگانه آپلود می‌شود
- ذخیره/حذف Keychain برای هر سرور ذخیره‌شده
- حذف پیام‌های حساس مثل پسورد تا جای ممکن
- میانبرهای Ctrl/Alt/Shift و دکمه‌های کاربردی ترمینال

## نصب

روی سرور لینوکسی اجرا کن:

```bash
sudo bash install.sh
```

یا فایل‌ها را در `/opt/sshbot` کپی کن و سرویس را با همین `install.sh` بساز.

بعد از نصب، برای امنیت حتماً در فایل زیر آیدی تلگرام خودت را در `ALLOWED_USERS` بگذار:

```bash
sudo nano /etc/sshbot.env
sudo systemctl restart sshbot
```

## استفاده سریع

### اتصال معمولی

- `/start`
- دکمه «اتصال»
- ارسال `user@host` یا `user@host:port`
- ارسال پسورد

### Keychain پیش‌فرض

برای اینکه فقط IP و Port بدهی و پسورد نخواهد:

1. دکمه «تنظیم Keychain» یا دستور `/keychain`
2. یوزرنیم SSH را بفرست، مثلاً `root`
3. پسورد را بفرست؛ پسورد رمزنگاری‌شده ذخیره می‌شود
4. دکمه «اتصال Keychain» یا دستور زیر را بزن:

```text
/quickssh 1.2.3.4:22
```

### Keychain برای سرور ذخیره‌شده

- از «افزودن سرور» استفاده کن و گزینه «ذخیره پسورد در Keychain» را انتخاب کن
- یا برای سرور قبلی:

```text
/serverpass NAME
```

بعد از ذخیره، با دکمه اتصال همان سرور دیگر پسورد نمی‌خواهد.

### آپلود چند فایل روی سرور

اول به سرور وصل شو، بعد:

```text
/upload /root/uploads
```

حالا در تلگرام چند فایل را با هم انتخاب و ارسال کن. ربات هر فایل را با SFTP روی همان مسیر آپلود می‌کند.

برای پایان حالت آپلود:

```text
/done
```

مسیر پیش‌فرض اگر چیزی ندهی `.` است، یعنی home/current SFTP کاربر SSH.

## تنظیمات مهم در `/etc/sshbot.env`

```env
KEYCHAIN_ENABLED=1
KEYCHAIN_KEY_FILE=/opt/sshbot/data/keychain.key
UPLOAD_TMP_DIR=/opt/sshbot/data/uploads
UPLOAD_CREATE_DIR=1
UPLOAD_OVERWRITE=1
MAX_UPLOAD_BYTES=0
DELETE_UPLOADED_TG_MESSAGE=0
PRIVATE_ONLY=1
ALLOWED_USERS=
```

نکته امنیتی: فایل `keychain.key` کلید رمزگشایی Keychain است. از آن بکاپ امن بگیر و اجازه دسترسی‌اش را محدود نگه دار.

## دستورهای اصلی

```text
/start
/menu
/help
/id
/status
/ssh user@host[:port]
/pass password
/stop
/servers
/addserver name user@host[:port]
/delserver name
/serverpass name
/keychain
/quickssh host[:port]
/upload [remote_dir]
/done
/ctrl c
/alt a
/shift x
/keys ctrl+alt+c
```
