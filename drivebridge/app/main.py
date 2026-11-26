import asyncio
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Request as WebRequest
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .google_drive import Drive, DriveError
from .store import Store

LOG = logging.getLogger('drivebridge')
logging.basicConfig(level=logging.INFO)
BASE = Path(__file__).parent
UPLOAD_CHUNK_LIMIT = 8 * 1024 * 1024
COOKIE_AGE = 24 * 3600


@dataclass
class Config:
    token: str
    owners: set[int]
    api_url: str
    telegram_root: str
    client_id: str
    client_secret: str
    public_url: str
    admin_password: str
    session_secret: str
    data_dir: str
    folder_id: str = ''
    max_upload_gb: float = 5
    secure_cookie: bool = True

    @classmethod
    def from_env(cls):
        return cls(
            token=os.environ['TELEGRAM_BOT_TOKEN'],
            owners={int(x.strip()) for x in os.environ['TELEGRAM_OWNER_ID'].split(',') if x.strip()},
            api_url=os.getenv('TELEGRAM_API_URL', 'http://telegram-api:8081').rstrip('/'),
            telegram_root=os.getenv('TELEGRAM_FILES_ROOT', '/var/lib/telegram-bot-api'),
            client_id=os.environ['GOOGLE_CLIENT_ID'],
            client_secret=os.environ['GOOGLE_CLIENT_SECRET'],
            public_url=os.environ['PUBLIC_BASE_URL'].rstrip('/'),
            admin_password=os.environ['ADMIN_PASSWORD'],
            session_secret=os.environ['SESSION_SECRET'],
            data_dir=os.getenv('DATA_DIR', '/data'),
            folder_id=os.getenv('GOOGLE_FOLDER_ID', ''),
            max_upload_gb=float(os.getenv('MAX_UPLOAD_GB', '5')),
            secure_cookie=os.getenv('SECURE_COOKIE', '1') == '1',
        )


def safe_name(raw):
    name = str(raw or 'file').replace('\\', '/').split('/')[-1]
    name = ''.join(c for c in name if c.isprintable() and c not in '<>"|:?*')
    return name.strip('. ')[:180] or 'file'


def bot_call(config, method, params=None, timeout=45):
    body = json.dumps(params or {}).encode()
    req = Request(config.api_url + '/bot' + config.token + '/' + method, data=body,
                  headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urlopen(req, timeout=timeout) as res:
            data = json.load(res)
    except Exception:
        # urllib errors can contain the bot token in the URL; do not log them.
        raise RuntimeError('Telegram Bot API unavailable') from None
    if not data.get('ok'):
        raise RuntimeError('Telegram: ' + str(data.get('description', 'unknown error')))
    return data['result']


def sign(config, text):
    return hmac.new(config.session_secret.encode(), text.encode(), hashlib.sha256).hexdigest()


def session_token(config, timestamp=None):
    payload = str(int(timestamp or time.time())) + '.' + secrets.token_urlsafe(24)
    return payload + '.' + sign(config, payload)


def valid_session(config, cookie):
    try:
        timestamp, nonce, signature = cookie.split('.', 2)
        value = timestamp + '.' + nonce
        age = time.time() - int(timestamp)
        return 0 <= age < COOKIE_AGE and hmac.compare_digest(signature, sign(config, value))
    except (AttributeError, ValueError):
        return False


def csrf(config, cookie):
    return sign(config, cookie + ':csrf')


def create_app(config=None):
    config = config or Config.from_env()
    if len(config.session_secret) < 32 or len(config.admin_password) < 12:
        raise ValueError('Set SESSION_SECRET >=32 characters and ADMIN_PASSWORD >=12 characters')
    if not config.public_url.startswith('https://') and config.secure_cookie:
        raise ValueError('PUBLIC_BASE_URL must be HTTPS')
    if not 0 < config.max_upload_gb <= 100:
        raise ValueError('MAX_UPLOAD_GB must be in the range 0..100')
    store = Store(config.data_dir)
    for owner in config.owners:
        store.seed_admin(owner)
    drive = Drive(config.data_dir, config.client_id, config.client_secret,
                  config.public_url, config.folder_id)
    upload_lock = asyncio.Lock()
    attempts = {}
    processing_tasks = set()

    async def send(chat, message):
        try:
            return await asyncio.to_thread(bot_call, config, 'sendMessage',
                                           {'chat_id': chat, 'text': message})
        except Exception:
            LOG.exception('Could not notify owner')

    def identity(message):
        person = message.get('from', {})
        name = ' '.join(filter(None, (person.get('first_name'), person.get('last_name')))).strip()
        return person.get('id'), person.get('username'), name

    async def admin_command(chat, who, command, argument):
        if command == '/panel':
            enabled = sum(bool(row['enabled']) for row in store.users())
            await send(chat, 'پنل مدیریت ربات\n\n'
                       f'کاربران فعال: {enabled}\n'
                       '/add 123456789 — افزودن با شناسه\n'
                       '/add @username — افزودن کاربری که قبلاً ربات را Start کرده\n'
                       '/del 123456789 — حذف دسترسی\n'
                       '/users — فهرست کاربران\n'
                       '/files — فایل‌های اخیر خودت')
            return True
        if command == '/users':
            rows = store.users()
            lines = ['کاربران ربات:']
            for row in rows:
                state = '✅' if row['enabled'] else '⛔️'
                admin = ' مدیر' if row['is_admin'] else ''
                username = (' @' + row['username']) if row['username'] else ''
                lines.append(f"{state} {row['telegram_id']}{username}{admin}")
            await send(chat, '\n'.join(lines))
            return True
        if command in ('/add', '/del'):
            target = argument.strip().split(' ', 1)[0] if argument.strip() else ''
            if not target:
                await send(chat, f'نمونه: {command} 5695376881')
                return True
            try:
                row = store.user(target)
            except ValueError:
                row = None
            if target.startswith('@') and not row:
                await send(chat, 'این نام کاربری هنوز شناخته نشده است. آن شخص ابتدا /start یا /id را به ربات بفرستد؛ سپس دوباره اضافه‌اش کن.')
                return True
            if row:
                target_id = row['telegram_id']
            else:
                try:
                    target_id = int(target)
                except ValueError:
                    await send(chat, 'شناسه معتبر نیست؛ شناسهٔ عددی یا @username بفرست.')
                    return True
            if command == '/del' and target_id in config.owners:
                await send(chat, 'مدیر اصلی را نمی‌توان از داخل ربات حذف کرد.')
                return True
            store.set_user_access(target_id, command == '/add')
            await send(chat, ('دسترسی فعال شد: ' if command == '/add' else 'دسترسی غیرفعال شد: ') + str(target_id))
            return True
        return False

    async def handle_message(message):
        who, username, display_name = identity(message)
        chat = message.get('chat', {}).get('id')
        if who is None or chat is None or message.get('chat', {}).get('type') != 'private':
            return
        store.remember_user(who, username, display_name)
        text = message.get('text', '').strip()
        parts = text.split(' ', 1)
        command = parts[0].split('@')[0].lower() if parts and parts[0].startswith('/') else ''
        argument = parts[1] if len(parts) > 1 else ''
        if command == '/id':
            await send(chat, f'شناسهٔ عددی شما: {who}')
            return
        account = store.user(who)
        if account and account['is_admin'] and await admin_command(chat, who, command, argument):
            return
        if not account or not account['enabled']:
            await send(chat, 'این ربات شخصی است.')
            return
        if command in ('/start', '/help'):
            await send(chat, 'فایل یا ویدیو را بفرست تا در پوشهٔ اختصاصی خودت در گوگل‌درایو ذخیره شود.\n/files — فایل‌های اخیر\n/id — شناسهٔ عددی')
            return
        if command == '/files':
            jobs = store.recent_user_jobs(who)
            if not jobs:
                await send(chat, 'هنوز فایلی در آرشیو تو ثبت نشده است.')
                return
            lines = ['فایل‌های اخیر تو:']
            for job in jobs:
                mark = '✅' if job['status'] == 'ready' else ('❌' if job['status'] == 'failed' else '⏳')
                line = f"{mark} {job['name']}"
                if job['drive_url']:
                    line += '\n' + job['drive_url']
                lines.append(line)
            await send(chat, '\n\n'.join(lines))
            return
        if not drive.connected():
            await send(chat, 'اول از پنل، گوگل‌درایو را وصل کن: ' + config.public_url)
            return
        media = next((message[key] for key in
                      ('document', 'video', 'animation', 'audio', 'voice', 'video_note', 'sticker')
                      if message.get(key)), None)
        if not media and message.get('photo'):
            media = message['photo'][-1]
        if not media or 'file_id' not in media:
            await send(chat, 'فایل یا ویدیو را برای من بفرست؛ /id شناسهٔ تلگرام شما را نشان می‌دهد.')
            return
        size = int(media.get('file_size', 0))
        filename = media.get('file_name') or f"telegram-{message['message_id']}"
        if message.get('photo') and media is message['photo'][-1]:
            filename += '.jpg'
        elif not media.get('file_name'):
            filename += mimetypes.guess_extension(media.get('mime_type', '')) or '.bin'
        filename = safe_name(filename)
        mime = media.get('mime_type') or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        try:
            info = await asyncio.to_thread(bot_call, config, 'getFile',
                                           {'file_id': media['file_id']}, 1800)
            source = Path(info['file_path']).resolve()
            root = Path(config.telegram_root).resolve()
            if not source.is_relative_to(root) or not source.is_file():
                raise ValueError('Telegram local file is unavailable or outside the shared volume')
            size = source.stat().st_size
            if size > int(config.max_upload_gb * 1024 ** 3):
                await send(chat, 'حجم فایل از سقف تعیین‌شده در تنظیمات بیشتر است.')
                return
            item_id = uuid.uuid4().hex
            store.enqueue(item_id, filename, mime, size, str(source), 'telegram', chat,
                          message['message_id'], who, username)
            await send(chat, f'«{filename}» به صف آپلود اضافه شد. بعد از پایان، لینک را همین‌جا می‌فرستم.')
        except sqlite3.IntegrityError:
            pass  # Telegram redelivered an update after a process restart.
        except Exception:
            LOG.exception('Could not queue Telegram file')
            await send(chat, 'دریافت فایل انجام نشد. وضعیت سرور محلی تلگرام را بررسی کن و دوباره بفرست.')

    async def poll_bot():
        offset = None
        while True:
            try:
                params = {'timeout': 25, 'allowed_updates': ['message']}
                if offset is not None:
                    params['offset'] = offset
                updates = await asyncio.to_thread(bot_call, config, 'getUpdates', params, 35)
                for update in updates:
                    offset = update['update_id'] + 1
                    try:
                        if 'message' in update:
                            await handle_message(update['message'])
                    except Exception:
                        LOG.exception('Telegram update failed')
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception('Telegram polling temporarily failed')
                await asyncio.sleep(5)

    async def processing_timer(chat, message_id, filename, link, transfer_seconds):
        for elapsed in range(30, 631, 30):
            await asyncio.sleep(30)
            text = (f'آپلود «{filename}» کامل شد.\n'
                    f'زمان انتقال: {max(1, round(transfer_seconds))} ثانیه\n'
                    f'زمان سپری‌شده از پردازش گوگل: {elapsed // 60}:{elapsed % 60:02d}\n'
                    'برای بررسی پخش، لینک را باز کن:\n' + link)
            try:
                await asyncio.to_thread(bot_call, config, 'editMessageText', {
                    'chat_id': chat, 'message_id': message_id, 'text': text,
                })
            except Exception:
                LOG.warning('Could not update Google processing timer')
                return

    def upload_job(job):
        started = time.time()
        progress_message = None
        last_notice = 0.0
        user = store.user(job['telegram_user']) if job['telegram_user'] else None
        folder_id = None
        if user:
            folder_id = user['drive_folder_id']
            if not folder_id:
                folder_id = drive.ensure_user_folder(
                    user['telegram_id'], user['username'] or user['display_name'])
                store.set_user_folder(user['telegram_id'], folder_id)

        def progress(uploaded, total):
            nonlocal progress_message, last_notice
            now = time.time()
            elapsed = max(now - started, 0.1)
            speed = uploaded / elapsed
            store.update_job(job['id'], uploaded=uploaded, speed=speed,
                             upload_started=started)
            if not job['telegram_chat'] or (uploaded < total and now - last_notice < 4):
                return
            percent = round(uploaded * 100 / total) if total else 100
            speed_mb = speed / 1024 / 1024
            text = (f"آپلود «{job['name']}» به گوگل‌درایو\n"
                    f"{percent}٪ · سرعت {speed_mb:.2f} مگابایت‌برثانیه")
            try:
                if progress_message is None:
                    result = bot_call(config, 'sendMessage',
                                      {'chat_id': job['telegram_chat'], 'text': text})
                    progress_message = result['message_id']
                else:
                    bot_call(config, 'editMessageText', {
                        'chat_id': job['telegram_chat'], 'message_id': progress_message,
                        'text': text,
                    })
                last_notice = now
            except Exception:
                LOG.warning('Could not update Telegram upload progress')

        existing = drive.find_job_file(job['id'])
        result = existing or drive.upload(job['source'], job['name'], job['mime'],
                                          job['size'], job['id'], folder_id, progress)
        file_id = result['id']
        link = result.get('webViewLink') or f'https://drive.google.com/file/d/{file_id}/view'
        finished = time.time()
        store.update_job(job['id'], status='ready', drive_id=file_id, drive_url=link,
                         uploaded=job['size'], upload_finished=finished, error=None)
        if job['source_kind'] == 'browser':
            Path(job['source']).unlink(missing_ok=True)
        return link, finished - started, progress_message

    async def worker():
        while True:
            try:
                job = store.next_job()
                if not job:
                    await asyncio.sleep(2)
                    continue
                try:
                    link, elapsed, progress_message = await asyncio.to_thread(upload_job, job)
                    if job['telegram_chat']:
                        message = ('آپلود «' + job['name'] + '» کامل شد.\n'
                                   f'زمان انتقال: {max(1, round(elapsed))} ثانیه\n'
                                   'گوگل اکنون نسخهٔ پخش ویدیو را پردازش می‌کند؛ زمان دقیق پایان را اعلام نمی‌کند.\n'
                                   'لینک خصوصی گوگل‌درایو: ' + link)
                        if progress_message:
                            try:
                                await asyncio.to_thread(bot_call, config, 'editMessageText', {
                                    'chat_id': job['telegram_chat'],
                                    'message_id': progress_message, 'text': message,
                                })
                            except Exception:
                                await send(job['telegram_chat'], message)
                        else:
                            result = await send(job['telegram_chat'], message)
                            progress_message = result.get('message_id') if result else None
                        if job['mime'].startswith('video/') and progress_message:
                            task = asyncio.create_task(processing_timer(
                                job['telegram_chat'], progress_message, job['name'], link, elapsed))
                            processing_tasks.add(task)
                            task.add_done_callback(processing_tasks.discard)
                except Exception as exc:
                    LOG.exception('Drive upload failed for job %s', job['id'])
                    store.update_job(job['id'], status='failed', error=str(exc)[:350])
                    if job['telegram_chat']:
                        await send(job['telegram_chat'], 'آپلود «' + job['name'] +
                                   '» ناموفق بود. از پنل وضعیت را ببین و دوباره تلاش کن.')
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception('Worker failed')
                await asyncio.sleep(5)

    @asynccontextmanager
    async def lifespan(app):
        store.reclaim_expired_uploads()
        tasks = [asyncio.create_task(poll_bot()), asyncio.create_task(worker())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in processing_tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*processing_tasks, return_exceptions=True)

    app = FastAPI(title='DriveBridge', docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.drive = drive

    @app.exception_handler(DriveError)
    async def drive_error_handler(req, exc):
        LOG.warning('Drive operation failed: %s', str(exc)[:200])
        return JSONResponse({'detail': 'خطا در گوگل‌درایو: ' + str(exc)[:300]},
                            status_code=502)

    def require_admin(req):
        cookie = req.cookies.get('db_session', '')
        if not valid_session(config, cookie):
            raise HTTPException(401, 'ابتدا وارد پنل شوید.')
        return cookie

    def require_csrf(req):
        cookie = require_admin(req)
        submitted = req.headers.get('x-csrf-token', '')
        if not hmac.compare_digest(submitted, csrf(config, cookie)):
            raise HTTPException(403, 'درخواست نامعتبر است؛ صفحه را تازه کنید.')

    def job_or_404(item_id):
        job = store.one('jobs', item_id)
        if not job:
            raise HTTPException(404, 'فایل پیدا نشد.')
        return job

    def public_job(row):
        return {key: row[key] for key in
                ('id', 'name', 'mime', 'size', 'status', 'drive_url', 'permission_id',
                 'error', 'created', 'uploaded', 'speed', 'upload_started',
                 'upload_finished', 'telegram_user', 'telegram_username')}

    @app.get('/')
    async def home():
        return FileResponse(BASE / 'index.html', media_type='text/html',
                            headers={'Cache-Control': 'no-store',
                                     'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"})

    @app.get('/app.js')
    async def script():
        return FileResponse(BASE / 'app.js', media_type='text/javascript',
                            headers={'Cache-Control': 'no-store'})

    @app.get('/health')
    async def health():
        return {'ok': True}

    @app.get('/api/me')
    async def me(req: WebRequest):
        cookie = require_admin(req)
        return {'csrf': csrf(config, cookie), 'connected': drive.connected(),
                'owners': sorted(config.owners)}

    @app.post('/api/login')
    async def login(req: WebRequest):
        ip = req.client.host if req.client else 'unknown'
        now = time.monotonic()
        recent = [t for t in attempts.get(ip, []) if now - t < 900]
        if len(recent) >= 8:
            raise HTTPException(429, 'تلاش‌های ناموفق زیاد است؛ کمی بعد دوباره امتحان کنید.')
        data = await req.json()
        if not hmac.compare_digest(str(data.get('password', '')), config.admin_password):
            recent.append(now)
            attempts[ip] = recent
            raise HTTPException(401, 'رمز عبور نادرست است.')
        attempts.pop(ip, None)
        res = JSONResponse({'ok': True})
        res.set_cookie('db_session', session_token(config), max_age=COOKIE_AGE,
                       secure=config.secure_cookie, httponly=True, samesite='lax')
        return res

    @app.post('/api/logout')
    async def logout(req: WebRequest):
        require_csrf(req)
        res = JSONResponse({'ok': True})
        res.delete_cookie('db_session')
        return res

    @app.get('/auth/google/start')
    async def google_start(req: WebRequest):
        require_admin(req)
        state = secrets.token_urlsafe(32)
        res = RedirectResponse(drive.authorization_url(state))
        res.set_cookie('oauth_state', state + '.' + sign(config, state),
                       max_age=600, secure=config.secure_cookie,
                       httponly=True, samesite='lax')
        return res

    @app.get('/auth/google/callback')
    async def google_callback(req: WebRequest, code: str = '', state: str = ''):
        require_admin(req)
        cookie = req.cookies.get('oauth_state', '')
        expected = state + '.' + sign(config, state)
        if not state or not hmac.compare_digest(cookie, expected) or not code:
            raise HTTPException(400, 'اتصال گوگل معتبر نیست؛ دوباره از پنل شروع کنید.')
        try:
            await asyncio.to_thread(drive.connect, code)
        except DriveError as exc:
            LOG.exception('Google OAuth failed')
            raise HTTPException(502, 'اتصال گوگل انجام نشد: ' + str(exc)) from exc
        res = RedirectResponse(config.public_url + '/')
        res.delete_cookie('oauth_state')
        return res

    @app.get('/api/jobs')
    async def list_jobs(req: WebRequest):
        require_admin(req)
        return {'files': [public_job(job) for job in store.jobs()]}

    @app.post('/api/uploads')
    async def start_upload(req: WebRequest):
        require_csrf(req)
        if not drive.connected():
            raise HTTPException(409, 'ابتدا گوگل‌درایو را وصل کنید.')
        payload = await req.json()
        try:
            size = int(payload['size'])
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, 'حجم فایل معتبر نیست.')
        if size < 0 or size > int(config.max_upload_gb * 1024 ** 3):
            raise HTTPException(413, 'حجم فایل از سقف مجاز بیشتر است.')
        name = safe_name(payload.get('name'))
        mime = str(payload.get('mime') or mimetypes.guess_type(name)[0] or
                   'application/octet-stream')[:120]
        item_id = uuid.uuid4().hex
        path = store.root / 'incoming' / (item_id + '.part')
        with path.open('xb'):
            pass
        with store.db() as db:
            db.execute('INSERT INTO uploads VALUES (?,?,?,?,?,?)',
                       (item_id, name, mime, size, 0, time.time()))
        return {'id': item_id, 'chunk_size': UPLOAD_CHUNK_LIMIT}

    @app.put('/api/uploads/{item_id}')
    async def append_upload(item_id: str, req: WebRequest, offset: int = 0):
        require_csrf(req)
        async with upload_lock:
            upload = store.one('uploads', item_id)
            if not upload:
                raise HTTPException(404, 'آپلود پیدا نشد یا منقضی شده است.')
            if offset != upload['received']:
                raise HTTPException(409, f"بخش بعدی باید از بایت {upload['received']} شروع شود.")
            content_length = req.headers.get('content-length')
            if content_length:
                try:
                    if int(content_length) > UPLOAD_CHUNK_LIMIT:
                        raise HTTPException(413, 'هر بخش باید حداکثر ۸ مگابایت باشد.')
                except ValueError:
                    raise HTTPException(400, 'طول بخش نامعتبر است.')
            path = store.root / 'incoming' / (item_id + '.part')
            written = 0
            with path.open('r+b') as stream:
                stream.seek(offset)
                try:
                    async for chunk in req.stream():
                        written += len(chunk)
                        if written > UPLOAD_CHUNK_LIMIT or offset + written > upload['size']:
                            raise HTTPException(413, 'حجم بخش معتبر نیست.')
                        stream.write(chunk)
                    if written == 0:
                        raise HTTPException(400, 'بخش خالی است.')
                    stream.flush()
                    os.fsync(stream.fileno())
                except BaseException:
                    stream.truncate(offset)
                    raise
            with store.db() as db:
                db.execute('UPDATE uploads SET received=? WHERE id=?',
                           (offset + written, item_id))
            return {'received': offset + written}

    @app.get('/api/uploads/{item_id}')
    async def upload_status(item_id: str, req: WebRequest):
        require_admin(req)
        upload = store.one('uploads', item_id)
        if not upload:
            raise HTTPException(404, 'آپلود پیدا نشد.')
        return {'received': upload['received'], 'size': upload['size']}

    @app.post('/api/uploads/{item_id}/finish')
    async def finish_upload(item_id: str, req: WebRequest):
        require_csrf(req)
        async with upload_lock:
            upload = store.one('uploads', item_id)
            if not upload:
                raise HTTPException(404, 'آپلود پیدا نشد.')
            if upload['size'] != upload['received']:
                raise HTTPException(409, 'فایل هنوز کامل دریافت نشده است.')
            path = store.root / 'incoming' / (item_id + '.part')
            if not path.is_file() or path.stat().st_size != upload['size']:
                raise HTTPException(409, 'فایل روی سرور کامل نیست.')
            now = time.time()
            with store.db() as db:
                db.execute('''INSERT INTO jobs
                    (id,name,mime,size,source,source_kind,created,updated)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (item_id, upload['name'], upload['mime'], upload['size'],
                     str(path), 'browser', now, now))
                db.execute('DELETE FROM uploads WHERE id=?', (item_id,))
            return {'id': item_id, 'status': 'pending'}

    @app.post('/api/jobs/{item_id}/retry')
    async def retry(item_id: str, req: WebRequest):
        require_csrf(req)
        job = job_or_404(item_id)
        if job['status'] != 'failed':
            raise HTTPException(409, 'فقط آپلود ناموفق قابل تکرار است.')
        if not Path(job['source']).is_file():
            raise HTTPException(409, 'فایل موقت موجود نیست؛ دوباره فایل را ارسال کنید.')
        store.update_job(item_id, status='pending', error=None)
        return {'ok': True}

    @app.post('/api/jobs/{item_id}/rename')
    async def rename(item_id: str, req: WebRequest):
        require_csrf(req)
        job = job_or_404(item_id)
        if job['status'] != 'ready':
            raise HTTPException(409, 'فایل هنوز آماده نیست.')
        name = safe_name((await req.json()).get('name'))
        await asyncio.to_thread(drive.rename, job['drive_id'], name)
        store.update_job(item_id, name=name)
        return {'ok': True}

    @app.post('/api/jobs/{item_id}/share')
    async def share(item_id: str, req: WebRequest):
        require_csrf(req)
        job = job_or_404(item_id)
        if job['status'] != 'ready':
            raise HTTPException(409, 'فایل هنوز آماده نیست.')
        if job['permission_id']:
            return {'ok': True, 'url': job['drive_url']}
        permission = await asyncio.to_thread(drive.share, job['drive_id'])
        store.update_job(item_id, permission_id=permission)
        return {'ok': True, 'url': job['drive_url']}

    @app.post('/api/jobs/{item_id}/unshare')
    async def unshare(item_id: str, req: WebRequest):
        require_csrf(req)
        job = job_or_404(item_id)
        if job['permission_id']:
            await asyncio.to_thread(drive.unshare, job['drive_id'], job['permission_id'])
            store.update_job(item_id, permission_id=None)
        return {'ok': True}

    @app.post('/api/jobs/{item_id}/trash')
    async def trash(item_id: str, req: WebRequest):
        require_csrf(req)
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=?', (item_id,)).fetchone()
            if not row:
                raise HTTPException(404, 'فایل پیدا نشد.')
            job = dict(row)
            if job['status'] in ('uploading', 'deleting'):
                raise HTTPException(409, 'صبر کنید عملیات فعلی کامل شود.')
            db.execute("UPDATE jobs SET status='deleting' WHERE id=?", (item_id,))
        try:
            if job['drive_id']:
                await asyncio.to_thread(drive.trash, job['drive_id'])
            if job['source_kind'] == 'browser':
                Path(job['source']).unlink(missing_ok=True)
            store.delete_job(item_id)
        except Exception:
            store.update_job(item_id, status=job['status'])
            raise
        return {'ok': True}

    return app


app = create_app()
