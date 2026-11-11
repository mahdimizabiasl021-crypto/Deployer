# ربات دیپلوی ورکر کلودفلر (بدون وبهوک)

ربات تلگرام با long polling. توکن Cloudflare و لینک ریپوی گیت‌هاب رو می‌گیره، منابع لازم (D1, KV, R2, Queue) رو می‌سازه و با wrangler دیپلوی می‌کنه.

## راه‌اندازی روی Render
1. از @BotFather یه ربات بساز و توکنش رو بردار.
2. این پوشه (bot.py, Dockerfile, render.yaml) رو توی یه ریپوی گیت‌هاب بذار.
3. Render → New → Web Service (یا Blueprint) → ریپو رو انتخاب کن، Runtime = Docker.
4. متغیرها:
   - `TELEGRAM_BOT_TOKEN` = توکن BotFather
   - `ALLOWED_USER_IDS` = آیدی عددی تلگرام خودت (چند تا با کاما). خالی بذاری همه می‌تونن استفاده کنن.
   - `GITHUB_TOKEN` (اختیاری) برای ریپوهای خصوصی
5. Deploy. بعد توی تلگرام /start بزن.

## نکته درباره‌ی خوابیدن
پلن رایگان Render سرویس وب رو بعد از ۱۵ دقیقه بی‌ترافیک می‌خوابونه و ربات polling هم متوقف میشه. ربات خودش هر ۱۰ دقیقه به آدرس عمومی خودش درخواست می‌زنه، ولی مطمئن‌تر اینه که یه مانیتور مثل UptimeRobot رو روی آدرس سرویس هر ۵ دقیقه بذاری، یا از Background Worker پولی استفاده کنی.

## چطور کار می‌کنه
- اگه ریپو `wrangler.toml / .json / .jsonc` داره از همون استفاده می‌کنه و فقط idهای خالی D1/KV رو با منابع جدید پر می‌کنه.
- اگه نداره، فایل ورود ورکر رو پیدا می‌کنه و از روی کد (`env.DB.prepare` → D1، `env.X.get/put` → KV، ...) باندینگ‌ها رو می‌سازه.
- `migrations/` با wrangler اجرا میشه؛ در نبودش `schema.sql` از طریق API.
- روت‌ها و دامنه‌ی سفارشی حذف میشن و ورکر روی workers.dev میاد.
- اگه کد `env.CF_API_TOKEN` / `CF_ACCOUNT_ID` / `WORKER_NAME` بخونه، خودش به‌عنوان secret ست میشن.
- Durable Object و Service binding رو فقط اگه توی wrangler config تعریف شده باشن پشتیبانی می‌کنه.
