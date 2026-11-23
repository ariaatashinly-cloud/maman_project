# Aria Drive Bridge

ربات شخصی تلگرام و پنل فارسی برای انتقال ویدیو و فایل به گوگل‌درایو. فایل ارسالی از تلگرام ابتدا روی VPS دریافت می‌شود، سپس با آپلود قابل ادامه به پوشهٔ اختصاصی برنامه در Drive می‌رود. فایل مرورگر از مسیر Cloudflare Tunnel به صورت بخش‌های ۸ مگابایتی ارسال می‌شود. تماشای آنلاین از لینک Google Drive انجام می‌شود.

## پیش‌نیاز

- VPS با Docker Engine و Docker Compose، فضای خالی متناسب با فایل‌ها و دسترسی خروجی سرور به Telegram و Google.
- یک دامنه در Cloudflare و یک Cloudflare Tunnel که دامنهٔ HTTPS را به سرویس `http://app:8000` وصل کند. **پورت ۸۰۰۰ یا ۸۰۸۱ را به اینترنت باز نکنید.**
- توکن ربات از [BotFather](https://t.me/BotFather)، و `api_id`/`api_hash` از [my.telegram.org](https://my.telegram.org).
- پروژهٔ Google Cloud با Drive API فعال و OAuth Client از نوع **Web application**. در «Authorized redirect URIs» آدرس دقیق `https://files.example.com/auth/google/callback` را ثبت کنید (دامنه را عوض کنید).

## راه‌اندازی روی سرور

۱. پوشهٔ پروژه را روی VPS منتقل کنید. ربات را با BotFather بسازید و قبل از روشن‌کردن برنامه یک پیام `/id` به آن بفرستید. برای یافتن شناسهٔ عددی خود:

```bash
python3 scripts/telegram_setup.py id
```

۲. تنظیمات را در یک فایل خصوصی بنویسید:

```bash
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
chmod 600 .env
```

خروجی دستور دوم را در `SESSION_SECRET` قرار دهید. برای `ADMIN_PASSWORD` یک رمز مستقل و طولانی انتخاب کنید. `TELEGRAM_OWNER_ID` همان شناسهٔ عددی مرحلهٔ ۱ است. `PUBLIC_BASE_URL` را برابر دامنهٔ HTTPS تونل بگذارید. `CF_TUNNEL_TOKEN`، `TELEGRAM_API_ID`، `TELEGRAM_API_HASH`، شناسه و رمز OAuth گوگل را در `.env` پر کنید. **توکن‌ها، رمز و فایل `.env` را در چت یا گیت‌هاب نفرستید.**

۳. در [Google Cloud Console](https://console.cloud.google.com/) یک پروژه بسازید و **Google Drive API** را فعال کنید. در تنظیمات Google Auth Platform صفحهٔ رضایت را بسازید؛ اگر وضعیت برنامه Testing است، حساب گوگل خود را به Test users اضافه کنید. OAuth Client از نوع Web application بسازید و URI بازگشت دقیق مرحلهٔ پیش‌نیاز را وارد کنید. شناسه و رمز Client را در `.env` بگذارید. برای استفادهٔ طولانی‌مدت، وضعیت Testing را به In production تغییر دهید؛ گوگل برای اپلیکیشن External در وضعیت Testing معمولاً refresh token با عمر ۷ روز صادر می‌کند.

۴. در Cloudflare Zero Trust یک **remotely managed Tunnel** بسازید، دامنهٔ `PUBLIC_BASE_URL` را به سرویس HTTP زیر وصل کنید:

```text
http://app:8000
```

توکن اجرای همان Tunnel را در `CF_TUNNEL_TOKEN` قرار دهید. نیاز به Cloudflare API Token یا R2 ندارید؛ Cloudflare در این پروژه فقط مسیر امن پنل و آپلود را فراهم می‌کند.

۵. **یک بار پیش از تغییر Bot API از ابری به محلی** دستور زیر را اجرا کنید:

```bash
python3 scripts/telegram_setup.py logout
```

۶. سرویس‌ها را بالا بیاورید و وضعیت را ببینید:

```bash
docker compose --profile tunnel up -d --build
docker compose ps
docker compose logs --tail=70 app telegram-api cloudflared
```

آدرس `PUBLIC_BASE_URL` را باز کنید، با `ADMIN_PASSWORD` وارد شوید، «اتصال حساب گوگل» را بزنید و دسترسی Drive را با همان حساب دلخواه تأیید کنید. حالا ویدیو را به ربات تلگرام بفرستید یا در پنل انتخاب کنید. پس از پایان آپلود، ربات لینک خصوصی Google Drive را ارسال می‌کند.

## نحوهٔ استفاده و محدودیت‌ها

- ربات فقط پیام خصوصی `TELEGRAM_OWNER_ID` را می‌پذیرد؛ دستور `/id` شناسهٔ شخص فرستنده را نشان می‌دهد.
- Google OAuth از مجوز `drive.file` استفاده می‌کند. برنامه در حالت عادی به همهٔ فایل‌های قبلی شما دسترسی ندارد؛ پوشهٔ `Telegram Drive Bridge` را در Drive ایجاد می‌کند و فایل‌های ساخته‌شدهٔ خودش را مدیریت می‌کند. `GOOGLE_FOLDER_ID` اختیاری است؛ پوشهٔ قدیمی باید برای همین برنامه قابل دسترس باشد، وگرنه آن را خالی بگذارید.
- لینک‌ها **به صورت پیش‌فرض خصوصی‌اند** و فقط برای حساب‌هایی که در Google Drive مجوز دارند باز می‌شوند. دکمهٔ «ساخت لینک عمومی» دسترسی «هرکس لینک را دارد» می‌سازد؛ «لغو لینک عمومی» همان مجوز را حذف می‌کند.
- «انتقال به سطل زباله» فایل Drive را به Trash می‌برد و ردیف آن را از پنل حذف می‌کند. امکان بازیابی از سطل زبالهٔ گوگل وجود دارد.
- API معمولی ربات تلگرام دانلود را به ۲۰ مگابایت محدود می‌کند. این بسته از Bot API محلی برای دریافت فایل‌های بزرگ استفاده می‌کند. محدودیت اندازهٔ فایل‌هایی که خود تلگرام قبول می‌کند همچنان برقرار است.
- Cloudflare در پلن Free هر درخواست آپلود را به ۱۰۰ مگابایت محدود می‌کند؛ پنل فایل را به بخش‌های ۸ مگابایتی تقسیم می‌کند. اگر صفحه بسته شود، آپلود مرورگر باید از اول شروع شود. انتقال سرور به Drive با خطای موقت قابل تکرار است.
- پخش ویدیو از **آدرس Google Drive** انجام می‌شود، نه از Cloudflare Tunnel؛ مقررات پلن‌های معمول Cloudflare برای تحویل ویدیو و فایل‌های حجیم از تونل عمومی محدودیت دارند. پردازش پیش‌نمایش ویدیو در Drive ممکن است پس از پایان آپلود کمی زمان ببرد. پخش داخل مرورگر به فرمت و کدک فایل هم وابسته است.
- فایل موقت مرورگر تا پایان موفق انتقال نگهداری می‌شود. فایل دریافتی از تلگرام در فضای Bot API محلی نیز ذخیره می‌شود. برای فضای دیسک VPS و گرفتن نسخهٔ پشتیبان از volume `app_data` برنامه‌ریزی کنید. رمز پنل، توکن ربات، فایل OAuth و volumeها خصوصی بمانند.

## بررسی محلی بدون سرویس خارجی

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -v
python3 -m compileall -q app
```

در اجرای محلیِ تست، درخواست واقعی به تلگرام یا گوگل ارسال نمی‌شود. تست نهایی اتصال باید پس از پرکردن تنظیمات روی VPS با حساب خودتان انجام شود.
