import csv
import hashlib
import hmac
import html
import io
import json
import os
import secrets
import sqlite3
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from app import annotations

ROOT = Path(__file__).parent
DATA = Path(os.getenv('DATA_DIR', './data'))
DATA.mkdir(parents=True, exist_ok=True)
(DATA / 'files').mkdir(exist_ok=True)
USERNAME = os.getenv('APP_USERNAME', 'admin')
PASSWORD = os.getenv('APP_PASSWORD', '')
if len(PASSWORD) < 12:
    raise RuntimeError('Задайте APP_PASSWORD длиной не менее 12 символов в .env')
SECURE = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
MAX_BYTES = 30 * 1024 * 1024
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

@contextmanager
def db():
    connection = sqlite3.connect(DATA / 'app.sqlite3', timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()

with db() as c:
    c.executescript('''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS documents (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
      created INTEGER NOT NULL, reviewed INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS histories (
      id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
      title TEXT NOT NULL, text TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS history_doc ON histories(document_id);
    CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, expires INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS attempts (ip TEXT PRIMARY KEY, count INTEGER, since INTEGER);
    ''')

with db() as c:
    annotations.initialize(c)

app.include_router(annotations.router(db))

def session_key(token):
    return hmac.new(PASSWORD.encode(), token.encode(), hashlib.sha256).hexdigest()

def authenticated(request):
    raw = request.cookies.get('session', '')
    if not raw:
        return False
    with db() as c:
        return c.execute('SELECT 1 FROM sessions WHERE token=? AND expires>?',
            (session_key(raw), int(time.time()))).fetchone() is not None

@app.middleware('http')
async def access(request: Request, call_next):
    path = request.url.path
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('x-requested-with') != 'Anamnesis':
        return JSONResponse({'detail': 'Некорректный запрос'}, status_code=403)
    if path.startswith('/api/') and path != '/api/login' and not authenticated(request):
        return JSONResponse({'detail': 'Войдите в аккаунт'}, status_code=401)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
    return response

app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')

@app.get('/')
def index():
    return FileResponse(ROOT / 'static/index.html')

class Login(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=500)

@app.post('/api/login')
def login(value: Login, request: Request):
    ip = request.client.host if request.client else 'unknown'
    now = int(time.time())
    with db() as c:
        c.execute('DELETE FROM attempts WHERE since<?', (now-600,))
        c.execute('DELETE FROM sessions WHERE expires<=?', (now,))
        attempt = c.execute('SELECT * FROM attempts WHERE ip=?', (ip,)).fetchone()
        if attempt and attempt['count'] >= 10:
            raise HTTPException(429, 'Слишком много попыток. Повторите через 10 минут.')
        valid = hmac.compare_digest(value.username.encode(), USERNAME.encode()) & hmac.compare_digest(value.password.encode(), PASSWORD.encode())
        if not valid:
            c.execute('INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(ip) DO UPDATE SET count=count+1', (ip,now))
            return JSONResponse({'detail': 'Неверный логин или пароль'}, status_code=401)
        c.execute('DELETE FROM attempts WHERE ip=?', (ip,))
        token = secrets.token_urlsafe(32)
        c.execute('INSERT INTO sessions VALUES(?,?)', (session_key(token), now+43200))
    response = JSONResponse({'username': USERNAME})
    response.set_cookie('session',token,httponly=True,secure=SECURE,samesite='lax',max_age=43200)
    return response

@app.get('/api/me')
def me():
    return {'username': USERNAME}

@app.post('/api/logout')
def logout(request: Request):
    with db() as c:
        c.execute('DELETE FROM sessions WHERE token=?', (session_key(request.cookies.get('session','')),))
    response = JSONResponse({'ok': True})
    response.delete_cookie('session')
    return response

def render_docx(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(i.file_size for i in archive.infolist()) > 100*1024*1024:
            raise ValueError('Распакованный документ слишком большой')
        if len(archive.infolist()) > 10000:
            raise ValueError('Слишком много частей документа')
    doc = Document(io.BytesIO(data))
    def block(node):
        if node.tag == qn('w:p'):
            chunks=[]
            for e in node.iter():
                if e.tag == qn('w:t'): chunks.append(html.escape(e.text or ''))
                elif e.tag == qn('w:tab'): chunks.append(' &nbsp; ')
                elif e.tag in (qn('w:br'),qn('w:cr')): chunks.append('<br>')
            text=''.join(chunks)
            if not text.strip(): return ''
            return '<p>'+text+'</p>'
        if node.tag == qn('w:tbl'):
            rows=[]
            for row in node.findall(qn('w:tr')):
                cells=[]
                for cell in row.findall(qn('w:tc')):
                    span=cell.find('./'+qn('w:tcPr')+'/'+qn('w:gridSpan'))
                    n=min(100,max(1,int(span.get(qn('w:val'),'1')))) if span is not None else 1
                    cells.append(f'<td colspan="{n}">'+''.join(block(child) for child in cell)+'</td>')
                rows.append('<tr>'+''.join(cells)+'</tr>')
            return '<table>'+''.join(rows)+'</table>'
        return ''
    return ''.join(block(node) for node in doc.element.body)

def get_document(identifier):
    with db() as c:
        row=c.execute('SELECT * FROM documents WHERE id=?',(identifier,)).fetchone()
    if row is None: raise HTTPException(404,'Документ не найден')
    return dict(row)

@app.get('/api/documents')
def documents():
    with db() as c:
        rows=c.execute('SELECT d.*, (SELECT COUNT(*) FROM histories h WHERE h.document_id=d.id) AS count FROM documents d ORDER BY created DESC, name').fetchall()
    return [dict(row) for row in rows]

@app.post('/api/documents', status_code=201)
def upload(file: UploadFile = File(...)):
    try:
        name=(file.filename or '').replace('\\','/').split('/')[-1]
        if not name.lower().endswith('.docx') or len(name)>240:
            raise HTTPException(400,'Загрузите документ .docx (имя до 240 символов)')
        data=file.file.read(MAX_BYTES+1)
        if not data or len(data)>MAX_BYTES: raise HTTPException(413,'Допустим непустой файл до 30 МБ')
        try: preview=render_docx(data)
        except Exception: raise HTTPException(400,'Не удалось прочитать DOCX. Файл повреждён, защищён паролем или слишком большой.')
        identifier=uuid.uuid4().hex
        target=DATA/'files'/identifier
        try:
            target.with_suffix('.docx').write_bytes(data)
            target.with_suffix('.html').write_text(preview,encoding='utf-8')
            with db() as c:
                c.execute('INSERT INTO documents(id,name,size,created) VALUES(?,?,?,?)',(identifier,name,len(data),int(time.time())))
        except Exception:
            target.with_suffix('.docx').unlink(missing_ok=True)
            target.with_suffix('.html').unlink(missing_ok=True)
            raise
        return get_document(identifier)
    finally: file.file.close()

@app.get('/api/documents/{identifier}/download')
def download(identifier: str):
    doc=get_document(identifier)
    return FileResponse(DATA/'files'/f'{identifier}.docx', filename=doc['name'])

@app.get('/api/documents/{identifier}/preview', response_class=HTMLResponse)
def preview(identifier: str):
    get_document(identifier)
    body=(DATA/'files'/f'{identifier}.html').read_text(encoding='utf-8')
    return '<!doctype html><html lang="ru"><head><meta charset="utf-8"><link rel="stylesheet" href="/static/document.css"><script src="/static/document.js" defer></script></head><body>'+body+'</body></html>'

class Reviewed(BaseModel):
    reviewed: bool

@app.patch('/api/documents/{identifier}')
def reviewed(identifier: str, value: Reviewed):
    get_document(identifier)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if value.reviewed:
            for row in c.execute('SELECT id FROM histories WHERE document_id=?',(identifier,)).fetchall(): annotations.require_values(c,row['id'])
        c.execute('UPDATE documents SET reviewed=?,workflow_status=? WHERE id=?',(int(value.reviewed),'done' if value.reviewed else 'in_progress',identifier))
    return {'ok':True}

class History(BaseModel):
    title: str = Field(default='Анамнез', max_length=150)
    text: str = Field(min_length=1, max_length=200000)
    values: dict = Field(default_factory=dict)

@app.get('/api/documents/{identifier}/histories')
def histories(identifier: str):
    get_document(identifier)
    with db() as c:
        c.execute('BEGIN')
        rows=c.execute('SELECT * FROM histories WHERE document_id=? ORDER BY created,id',(identifier,)).fetchall()
        return [{**dict(row),'values':annotations.values(c,row['id'])} for row in rows]

@app.post('/api/documents/{identifier}/histories', status_code=201)
def add(identifier: str, value: History):
    get_document(identifier)
    if not value.text.strip(): raise HTTPException(400,'Введите текст анамнеза')
    key=uuid.uuid4().hex; now=int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('INSERT INTO histories(id,document_id,title,text,created,updated) VALUES(?,?,?,?,?,?)',(key,identifier,value.title.strip() or 'Анамнез',value.text.strip(),now,now))
        annotations.save_values(c,key,value.values)
        annotations.reopen_document(c,identifier)
    return {'id':key}

@app.put('/api/histories/{identifier}')
def edit(identifier: str, value: History):
    if not value.text.strip(): raise HTTPException(400,'Введите текст анамнеза')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        previous=annotations.get_history(c,identifier)
        changed=previous['text'] != value.text.strip()
        if changed and c.execute('SELECT 1 FROM annotations WHERE history_id=?',(identifier,)).fetchone():
            raise HTTPException(409,'Текст содержит разметку. Сначала удалите её в разделе «Разметка», чтобы не сместить позиции.')
        c.execute('UPDATE histories SET title=?,text=?,updated=? WHERE id=?',
            (value.title.strip() or 'Анамнез',value.text.strip(),int(time.time()),identifier))
        annotations.save_values(c,identifier,value.values)
        if changed or previous['values']!=annotations.values(c,identifier):
            annotations.reopen(c,identifier)
            annotations.reopen_document(c,previous['document_id'])
    return {'ok':True}

@app.delete('/api/histories/{identifier}')
def delete(identifier: str):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=annotations.get_history(c,identifier)
        c.execute('DELETE FROM histories WHERE id=?',(identifier,))
        annotations.reopen_document(c,old['document_id'])
    return {'ok':True}

@app.get('/api/export/{kind}')
def export(kind: str, status: str = 'all'):
    if status not in {'all','pending','completed'}: raise HTTPException(400,'Неизвестный статус')
    with db() as c:
        c.execute('BEGIN')
        rows=[dict(r) for r in c.execute('''SELECT h.id,h.document_id,d.name AS document_name,h.title,h.text,h.created,h.updated,h.workflow_status,d.workflow_status AS document_status,
            COALESCE(r.completed,0) AS completed FROM histories h JOIN documents d ON d.id=h.document_id
            LEFT JOIN annotation_reviews r ON r.history_id=h.id ORDER BY d.name,h.created''')]
        all_spans=annotations.annotation_rows(c)
        by_history={}
        for span in all_spans: by_history.setdefault(span['history_id'],[]).append(span)
        for row in rows:
            row['annotations']=by_history.get(row['id'],[])
            stored=annotations.values(c,row['id'])
            row['columns']=[{'label_id':col['id'],'code':col['code'],'name':col['name'],'data_type':col['data_type'],'active':bool(col['active']),'value':stored.get(col['id'])} for col in annotations.columns(c)]
            row['schema_revision']=annotations.revision(c)
            row['offset_unit']='unicode_codepoint'
            row['end_exclusive']=True
        if status!='all': rows=[r for r in rows if bool(r['completed'])==(status=='completed')]
    if kind=='json':
        content=json.dumps(rows,ensure_ascii=False,indent=2).encode()
        media='application/json'
    elif kind=='csv':
        stream=io.StringIO(newline=''); fields=['id','document_id','document_name','title','text','created','updated','workflow_status','document_status','completed','annotations','columns','schema_revision','offset_unit','end_exclusive']
        writer=csv.DictWriter(stream,fieldnames=fields,delimiter=';');writer.writeheader()
        for row in rows:
            row={**row,'annotations':json.dumps(row['annotations'],ensure_ascii=False),'columns':json.dumps(row['columns'],ensure_ascii=False)}
            # Prevent spreadsheet formula interpretation of user-entered text.
            writer.writerow({k:("'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v) for k,v in row.items()})
        content=stream.getvalue().encode('utf-8-sig');media='text/csv'
    else: raise HTTPException(404)
    return Response(content,media_type=media,headers={'Content-Disposition':f'attachment; filename="anamneses.{kind}"'})
