const $ = (id) => document.getElementById(id);
let csrfToken = '';
let files = [];

function note(id, message) {
  const node = $(id);
  node.textContent = message;
  node.classList.toggle('hidden', !message);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !(options.body instanceof Blob)) headers['Content-Type'] = 'application/json';
  if (options.method && options.method !== 'GET') headers['X-CSRF-Token'] = csrfToken;
  const res = await fetch(path, { ...options, headers, credentials: 'same-origin' });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw Error(body.detail || `خطای سرور (${res.status})`);
  return body;
}

function formatSize(size) {
  const units = ['بایت', 'کیلوبایت', 'مگابایت', 'گیگابایت'];
  let i = 0;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
  return `${size.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function formatDuration(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
}

function makeButton(label, action, style = 'quiet') {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = style;
  button.textContent = label;
  button.addEventListener('click', async () => {
    button.disabled = true;
    try { await action(); } catch (err) { note('message', err.message); }
    finally { button.disabled = false; }
  });
  return button;
}

function render() {
  const filter = $('search').value.trim().toLocaleLowerCase();
  const root = $('files');
  root.replaceChildren();
  const visible = files.filter(f => f.name.toLocaleLowerCase().includes(filter));
  if (!visible.length) { const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = 'فایلی برای نمایش نیست.'; root.append(empty); }
  for (const file of visible) {
    const card = document.createElement('div'); card.className = 'file';
    const top = document.createElement('div'); top.className = 'row space';
    const name = document.createElement('div'); name.className = 'file-name'; name.textContent = file.name;
    const status = document.createElement('span'); status.className = `status ${file.status}`;
    status.textContent = ({ready: 'آماده', pending: 'در صف', uploading: 'در حال انتقال', failed: 'ناموفق'})[file.status] || file.status;
    top.append(name, status); card.append(top);
    const meta = document.createElement('div'); meta.className = 'file-meta';
    meta.textContent = `${formatSize(file.size)} · ${new Intl.DateTimeFormat('fa-IR', {dateStyle:'medium', timeStyle:'short'}).format(new Date(file.created * 1000))}`;
    card.append(meta);
    if (file.status === 'uploading') {
      const progress = document.createElement('div'); progress.className = 'file-meta';
      const percent = file.size ? Math.round(100 * (file.uploaded || 0) / file.size) : 0;
      progress.textContent = `${percent}٪ · سرعت ${formatSize(file.speed || 0)}/ثانیه`;
      card.append(progress);
    }
    if (file.status === 'ready' && file.mime.startsWith('video/') && file.upload_finished) {
      const processing = document.createElement('div'); processing.className = 'file-meta';
      processing.textContent = `زمان گذشته از شروع پردازش گوگل: ${formatDuration(Date.now() / 1000 - file.upload_finished)}`;
      card.append(processing);
    }
    if (file.error) {const err = document.createElement('p'); err.textContent = file.error; card.append(err);}
    const actions = document.createElement('div'); actions.className = 'row';
    if (file.status === 'ready') {
      const link = document.createElement('a'); link.href = file.drive_url; link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.textContent = file.mime.startsWith('video/') ? 'تماشا در گوگل‌درایو ↗' : 'باز کردن در گوگل‌درایو ↗'; actions.append(link);
      actions.append(makeButton('کپی لینک', async () => {
        await navigator.clipboard.writeText(file.drive_url);
        note('message', file.permission_id ? 'لینک عمومی کپی شد.' : 'لینک خصوصی کپی شد؛ فقط افراد دارای دسترسی به این فایل می‌توانند آن را باز کنند.');
      }));
      actions.append(makeButton('تغییر نام', async () => {
        const value = prompt('نام جدید فایل:', file.name);
        if (value === null || !value.trim()) return;
        await api(`/api/jobs/${file.id}/rename`, {method:'POST', body:JSON.stringify({name:value})}); await refresh();
      }));
      if (file.permission_id) actions.append(makeButton('لغو لینک عمومی', async () => {
        if (!confirm('دسترسی عمومی این فایل لغو شود؟')) return;
        await api(`/api/jobs/${file.id}/unshare`, {method:'POST'}); await refresh();
      }));
      else actions.append(makeButton('ساخت لینک عمومی', async () => {
        if (!confirm('هرکس این لینک را داشته باشد می‌تواند فایل را ببیند. ادامه می‌دهید؟')) return;
        await api(`/api/jobs/${file.id}/share`, {method:'POST'}); await refresh();
        note('message', 'دسترسی عمومی فعال شد. حالا لینک فایل را کپی کنید.');
      }));
    }
    if (file.status === 'failed') actions.append(makeButton('تلاش دوباره', async () => {
      await api(`/api/jobs/${file.id}/retry`, {method:'POST'}); await refresh();
    }));
    if (file.status !== 'uploading') actions.append(makeButton('انتقال به سطل زباله', async () => {
      if (!confirm(`«${file.name}» به سطل زبالهٔ گوگل‌درایو منتقل شود؟`)) return;
      await api(`/api/jobs/${file.id}/trash`, {method:'POST'}); await refresh();
    }, 'danger'));
    card.append(actions); root.append(card);
  }
}

async function refresh() {
  const data = await api('/api/jobs'); files = data.files; render();
}

async function initialize() {
  try {
    const me = await api('/api/me'); csrfToken = me.csrf;
    $('login-panel').classList.add('hidden'); $('dashboard').classList.remove('hidden'); $('logout').classList.remove('hidden');
    $('drive-status').textContent = me.connected ? 'حساب گوگل متصل است ✓' : 'حساب گوگل هنوز متصل نشده است.';
    $('connect-link').querySelector('button').textContent = me.connected ? 'اتصال دوباره' : 'اتصال حساب گوگل';
    await refresh();
  } catch {
    $('login-panel').classList.remove('hidden'); $('dashboard').classList.add('hidden'); $('logout').classList.add('hidden');
  }
}

async function upload(file) {
  if (!file) return;
  $('file-picker').disabled = true;
  $('progress').classList.remove('hidden'); $('progress-bar').style.width = '0%';
  try {
    note('upload-status', `آماده‌سازی «${file.name}»…`);
    const task = await api('/api/uploads', {method:'POST', body:JSON.stringify({name:file.name, size:file.size, mime:file.type})});
    let offset = 0;
    while (offset < file.size) {
      const piece = file.slice(offset, offset + task.chunk_size);
      for (let attempt = 0; attempt < 5; attempt++) {
        try {
          const result = await api(`/api/uploads/${task.id}?offset=${offset}`, {method:'PUT', body:piece});
          offset = result.received;
          break;
        } catch (err) {
          const status = await api(`/api/uploads/${task.id}`);
          if (status.received > offset) {offset = status.received; break;}
          if (attempt === 4) throw err;
          await new Promise(resolve => setTimeout(resolve, 1000 * (attempt + 1)));
        }
      }
      $('progress-bar').style.width = `${Math.round(100 * offset / file.size)}%`;
      note('upload-status', `در حال انتقال «${file.name}» به سرور: ${Math.round(100 * offset / file.size)}٪`);
    }
    await api(`/api/uploads/${task.id}/finish`, {method:'POST'});
    $('progress-bar').style.width = '100%';
    note('upload-status', 'فایل به صف انتقال به گوگل‌درایو اضافه شد.');
    await refresh();
  } catch (err) { note('upload-status', err.message); }
  finally { $('file-picker').disabled = false; $('file-picker').value = ''; }
}

$('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const res = await fetch('/api/login', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:$('password').value})});
    if (!res.ok) throw Error((await res.json()).detail || 'خطا در ورود');
    $('password').value = ''; note('login-message', ''); await initialize();
  } catch (err) {note('login-message', err.message);}
});
$('logout').addEventListener('click', async () => {await api('/api/logout', {method:'POST'}); csrfToken=''; await initialize();});
$('refresh').addEventListener('click', () => refresh().catch(err => note('message', err.message)));
$('search').addEventListener('input', render);
$('file-picker').addEventListener('change', event => upload(event.target.files[0]));
initialize();
setInterval(() => {if (csrfToken) refresh().catch(() => {});}, 10000);
setInterval(() => {if (csrfToken) render();}, 1000);
