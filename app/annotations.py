"""Typed dataset columns, history values, span results and workflow statuses."""
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StrictInt


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def add_column(c,table,name,definition):
    if name not in {r[1] for r in c.execute(f'PRAGMA table_info({table})')}:
        c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        return True
    return False


def initialize(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS labels (
      id TEXT PRIMARY KEY,code TEXT NOT NULL UNIQUE,name TEXT NOT NULL,color TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS annotation_reviews (
      history_id TEXT PRIMARY KEY REFERENCES histories(id) ON DELETE CASCADE,completed INTEGER NOT NULL DEFAULT 0,updated INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS annotations (
      id TEXT PRIMARY KEY,history_id TEXT NOT NULL REFERENCES histories(id) ON DELETE CASCADE,
      label_id TEXT NOT NULL REFERENCES labels(id),start INTEGER NOT NULL,end INTEGER NOT NULL,
      text TEXT NOT NULL,created INTEGER NOT NULL,CHECK(start>=0 AND end>start),UNIQUE(history_id,label_id,start,end));
    CREATE INDEX IF NOT EXISTS annotation_history ON annotations(history_id);
    CREATE TABLE IF NOT EXISTS dataset_meta (key TEXT PRIMARY KEY,value INTEGER NOT NULL);
    INSERT OR IGNORE INTO dataset_meta VALUES('revision',1);
    CREATE TABLE IF NOT EXISTS history_values (
      history_id TEXT NOT NULL REFERENCES histories(id) ON DELETE CASCADE,
      label_id TEXT NOT NULL REFERENCES labels(id),value TEXT NOT NULL,
      PRIMARY KEY(history_id,label_id));
    ''')
    upgraded=add_column(c,'labels','data_type',"TEXT NOT NULL DEFAULT 'enum'")
    add_column(c,'labels','options',"TEXT NOT NULL DEFAULT '[\"упоминание\"]'")
    add_column(c,'annotations','result','TEXT')
    add_column(c,'histories','workflow_status',"TEXT NOT NULL DEFAULT 'new'")
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'").fetchone():
        add_column(c,'documents','workflow_status',"TEXT NOT NULL DEFAULT 'new'")
    seeds=json.loads(Path(__file__).with_name('seed_labels.json').read_text(encoding='utf-8'))
    added=False
    for item in seeds:
        inserted=c.execute('INSERT OR IGNORE INTO labels(id,code,name,color,data_type,options) VALUES(?,?,?,?,?,?)',
          (uuid.uuid4().hex,item['code'],item['name'],item['color'],item['data_type'],json.dumps(item['options'],ensure_ascii=False)))
        added=added or bool(inserted.rowcount)
        if upgraded:
            c.execute('UPDATE labels SET data_type=?,options=? WHERE code=?',
                (item['data_type'],json.dumps(item['options'],ensure_ascii=False),item['code']))
    if upgraded:
        c.execute('UPDATE annotation_reviews SET completed=0')
    if added and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'").fetchone():
        invalidate_schema(c)


def revision(c):
    return c.execute("SELECT value FROM dataset_meta WHERE key='revision'").fetchone()[0]


def columns(c):
    return [{**dict(r),'options':json.loads(r['options'])} for r in c.execute('SELECT * FROM labels ORDER BY code')]


def invalidate_schema(c):
    c.execute("UPDATE dataset_meta SET value=value+1 WHERE key='revision'")
    c.execute("UPDATE documents SET workflow_status='in_progress',reviewed=0 WHERE workflow_status='done'")
    c.execute("UPDATE histories SET workflow_status='in_progress' WHERE workflow_status='done'")
    c.execute('UPDATE annotation_reviews SET completed=0,updated=?',(int(time.time()),))


def check_revision(c,value):
    if value!=revision(c): raise HTTPException(409,'Колонки изменились. Откройте запись заново перед завершением.')


def validate_value(column,value):
    kind=column['data_type']
    if kind=='int' and (type(value) is not int or abs(value)>9007199254740991):
        raise HTTPException(400,'Колонка «'+column['name']+'» требует целое число в диапазоне ±(2^53−1)')
    if kind=='bool' and type(value) is not bool:
        raise HTTPException(400,'Колонка «'+column['name']+'» требует да/нет')
    if kind=='enum' and (type(value) is not str or value not in column['options']):
        raise HTTPException(400,'Выберите значение колонки «'+column['name']+'» из списка')
    return value


def values(c,identifier):
    return {r['label_id']:json.loads(r['value']) for r in c.execute('SELECT * FROM history_values WHERE history_id=?',(identifier,))}


def value_states(c,identifier):
    stored=values(c,identifier)
    return {col['id']:('unreviewed' if col['id'] not in stored else 'no_data' if stored[col['id']] is None else 'value') for col in columns(c)}


def save_values(c,identifier,entered,states=None):
    states=states or {}
    active={col['id']:col for col in columns(c) if col['active']}
    for label_id in entered.keys() | states.keys():
        if label_id not in active: raise HTTPException(400,'Колонка удалена или отключена. Обновите страницу.')
        value=entered.get(label_id)
        state=states.get(label_id,'unreviewed' if value is None else 'value')
        if state in {'no_data','unreviewed'} and value is not None:
            raise HTTPException(400,'Состояние колонки не соответствует её значению')
        if state=='unreviewed': c.execute('DELETE FROM history_values WHERE history_id=? AND label_id=?',(identifier,label_id))
        else:
            if state=='value': validate_value(active[label_id],value)
            # JSON null is an explicitly checked absence; no row means unreviewed.
            c.execute('INSERT INTO history_values VALUES(?,?,?) ON CONFLICT(history_id,label_id) DO UPDATE SET value=excluded.value',
                (identifier,label_id,json.dumps(value,ensure_ascii=False)))


def require_values(c,identifier):
    stored=values(c,identifier)
    for col in columns(c):
        if not col['active']: continue
        if col['id'] not in stored: raise HTTPException(400,'Проверьте колонку «'+col['name']+'»: укажите значение или выберите «Нет данных»')
        if stored[col['id']] is not None: validate_value(col,stored[col['id']])


def reopen_document(c,document_id):
    c.execute("UPDATE documents SET workflow_status='in_progress',reviewed=0 WHERE id=?",(document_id,))


def reopen(c,identifier):
    c.execute('INSERT INTO annotation_reviews VALUES(?,0,?) ON CONFLICT(history_id) DO UPDATE SET completed=0,updated=excluded.updated',(identifier,int(time.time())))
    c.execute("UPDATE histories SET workflow_status='in_progress' WHERE id=?",(identifier,))


def annotation_rows(c,history_id=None):
    query='''SELECT a.id,a.history_id,h.document_id,d.name AS document_name,l.id AS label_id,l.code AS label,
      l.name AS label_name,l.color,l.data_type,a.start,a.end,a.text,a.result,a.created
      FROM annotations a JOIN labels l ON l.id=a.label_id JOIN histories h ON h.id=a.history_id
      JOIN documents d ON d.id=h.document_id'''
    if history_id is not None: query+=' WHERE a.history_id=?'
    rows=[dict(r) for r in c.execute(query+' ORDER BY a.start,a.end,a.created,a.id',() if history_id is None else (history_id,))]
    for row in rows:
        raw=row['result']
        row['result']=json.loads(raw) if raw is not None else None
        row['result_state']='unreviewed' if raw is None else 'no_data' if row['result'] is None else 'value'
    return rows


def get_history(c,identifier):
    row=c.execute('''SELECT h.*,d.name AS document_name,COALESCE(r.completed,0) AS completed
      FROM histories h JOIN documents d ON d.id=h.document_id
      LEFT JOIN annotation_reviews r ON r.history_id=h.id WHERE h.id=?''',(identifier,)).fetchone()
    if row is None: raise HTTPException(404,'Анамнез не найден')
    return {**dict(row),'text_hash':text_hash(row['text']),'values':values(c,identifier),'value_states':value_states(c,identifier),'schema_revision':revision(c)}


def check_version(history,expected):
    if history['text_hash']!=expected: raise HTTPException(409,'Текст изменился. Откройте анамнез заново.')


class Label(BaseModel):
    code: str=Field(pattern=r'^[A-Z][A-Z0-9_]{0,63}$')
    name: str=Field(min_length=1,max_length=100)
    color: str=Field(default='#f5cc76',pattern=r'^#[0-9a-fA-F]{6}$')
    data_type: Literal['int','bool','enum']='enum'
    options: list[str]=Field(default_factory=lambda:['упоминание'],max_length=100)

class LabelChange(BaseModel):
    name: str=Field(min_length=1,max_length=100)
    color: str=Field(pattern=r'^#[0-9a-fA-F]{6}$')
    active: bool=True

class Span(BaseModel):
    label_id: str
    start: StrictInt=Field(ge=0)
    end: StrictInt=Field(gt=0)
    text: str=Field(min_length=1,max_length=200000)
    text_hash: str
    result: Any
    result_state: Literal['value','no_data','unreviewed']='value'


def span_result(column,value):
    if value.result_state=='value':
        validate_value(column,value.result)
    elif value.result is not None:
        raise HTTPException(400,'Состояние фрагмента не соответствует его результату')
    return None if value.result_state=='unreviewed' else json.dumps(value.result,ensure_ascii=False)

class Version(BaseModel):
    text_hash: str

class Completion(Version):
    completed: bool
    schema_revision: int

class DocumentStatus(BaseModel):
    status: Literal['new','in_progress','done']
    schema_revision: int


def router(db):
    api=APIRouter(prefix='/api')

    @api.get('/labels')
    def labels():
        with db() as c: return columns(c)

    @api.get('/schema')
    def schema():
        with db() as c: return {'revision':revision(c),'columns':columns(c)}

    @api.post('/labels',status_code=201)
    def create_label(value: Label):
        options=[o.strip() for o in value.options]
        if not value.name.strip(): raise HTTPException(400,'Введите название')
        if value.data_type=='enum' and (not options or any(not o or len(o)>150 for o in options) or len(set(options))!=len(options)):
            raise HTTPException(400,'Укажите непустые уникальные варианты списка (до 150 символов)')
        key=uuid.uuid4().hex
        try:
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute('INSERT INTO labels(id,code,name,color,data_type,options) VALUES(?,?,?,?,?,?)',
                    (key,value.code,value.name.strip(),value.color,value.data_type,json.dumps(options if value.data_type=='enum' else [],ensure_ascii=False)))
                invalidate_schema(c)
        except sqlite3.IntegrityError: raise HTTPException(409,'Код колонки уже существует')
        return {'id':key}

    @api.patch('/labels/{identifier}')
    def change_label(identifier: str,value: LabelChange):
        if not value.name.strip(): raise HTTPException(400,'Введите название')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM labels WHERE id=?',(identifier,)).fetchone()
            if not row: raise HTTPException(404,'Колонка не найдена')
            c.execute('UPDATE labels SET name=?,color=?,active=? WHERE id=?',(value.name.strip(),value.color,int(value.active),identifier))
            if bool(row['active'])!=value.active: invalidate_schema(c)
        return {'ok':True}

    @api.post('/documents/{identifier}/open')
    def open_document(identifier: str):
        with db() as c:
            if not c.execute('SELECT 1 FROM documents WHERE id=?',(identifier,)).fetchone(): raise HTTPException(404,'Документ не найден')
            c.execute("UPDATE documents SET workflow_status='in_progress' WHERE id=? AND workflow_status='new'",(identifier,))
        return {'ok':True}

    @api.patch('/documents/{identifier}/status')
    def document_status(identifier: str,value: DocumentStatus):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('SELECT 1 FROM documents WHERE id=?',(identifier,)).fetchone(): raise HTTPException(404,'Документ не найден')
            check_revision(c,value.schema_revision)
            if value.status=='done':
                for row in c.execute('SELECT id FROM histories WHERE document_id=?',(identifier,)).fetchall(): require_values(c,row['id'])
            c.execute('UPDATE documents SET workflow_status=?,reviewed=? WHERE id=?',(value.status,int(value.status=='done'),identifier))
        return {'ok':True}

    @api.get('/histories')
    def list_histories(status: Literal['all','pending','completed']='all'):
        with db() as c:
            query='''SELECT h.id,h.document_id,h.title,h.text,h.workflow_status,d.name AS document_name,
              COALESCE(r.completed,0) AS completed,(SELECT COUNT(*) FROM annotations a WHERE a.history_id=h.id) AS annotation_count
              FROM histories h JOIN documents d ON d.id=h.document_id LEFT JOIN annotation_reviews r ON r.history_id=h.id'''
            if status!='all': query+=' WHERE COALESCE(r.completed,0)='+('1' if status=='completed' else '0')
            return [dict(r) for r in c.execute(query+' ORDER BY h.created,h.id')]

    @api.post('/histories/{identifier}/open')
    def open_history(identifier: str):
        with db() as c:
            get_history(c,identifier)
            c.execute("UPDATE histories SET workflow_status='in_progress' WHERE id=? AND workflow_status='new'",(identifier,))
        return {'ok':True}

    @api.get('/histories/{identifier}/annotation')
    def detail(identifier: str):
        with db() as c:
            c.execute('BEGIN')
            result=get_history(c,identifier);result['annotations']=annotation_rows(c,identifier)
            return result

    @api.post('/histories/{identifier}/annotations',status_code=201)
    def add_span(identifier: str,value: Span):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            h=get_history(c,identifier);check_version(h,value.text_hash)
            if value.start>=value.end or value.end>len(h['text']): raise HTTPException(400,'Позиции вне текста')
            if h['text'][value.start:value.end]!=value.text or not value.text.strip(): raise HTTPException(400,'Фрагмент не совпадает с позициями')
            col=next((l for l in columns(c) if l['id']==value.label_id and l['active']),None)
            if col is None: raise HTTPException(400,'Выберите активную колонку')
            result=span_result(col,value)
            key=uuid.uuid4().hex
            try:
                c.execute('INSERT INTO annotations(id,history_id,label_id,start,end,text,created,result) VALUES(?,?,?,?,?,?,?,?)',
                    (key,identifier,value.label_id,value.start,value.end,value.text,int(time.time()),result))
            except sqlite3.IntegrityError: raise HTTPException(409,'Этот фрагмент уже размечен этой колонкой')
            reopen(c,identifier)
        return {'id':key}

    @api.patch('/histories/{identifier}/annotations/{annotation_id}')
    def update_result(identifier: str,annotation_id: str,value: Span):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            h=get_history(c,identifier);check_version(h,value.text_hash)
            row=c.execute('SELECT * FROM annotations WHERE id=? AND history_id=?',(annotation_id,identifier)).fetchone()
            if not row: raise HTTPException(404,'Фрагмент не найден')
            if (row['label_id'],row['start'],row['end'],row['text'])!=(value.label_id,value.start,value.end,value.text): raise HTTPException(400,'Этот запрос меняет только результат фрагмента')
            col=next(l for l in columns(c) if l['id']==value.label_id)
            result=span_result(col,value)
            c.execute('UPDATE annotations SET result=? WHERE id=?',(result,annotation_id));reopen(c,identifier)
        return {'ok':True}

    @api.delete('/histories/{identifier}/annotations/{annotation_id}')
    def delete_span(identifier: str,annotation_id: str):
        with db() as c:
            c.execute('BEGIN IMMEDIATE');get_history(c,identifier)
            if not c.execute('DELETE FROM annotations WHERE id=? AND history_id=?',(annotation_id,identifier)).rowcount: raise HTTPException(404,'Фрагмент не найден')
            reopen(c,identifier)
        return {'ok':True}

    @api.post('/histories/{identifier}/annotations/reset')
    def reset(identifier: str,value: Version):
        with db() as c:
            c.execute('BEGIN IMMEDIATE');check_version(get_history(c,identifier),value.text_hash)
            c.execute('DELETE FROM annotations WHERE history_id=?',(identifier,));reopen(c,identifier)
        return {'ok':True}

    @api.patch('/histories/{identifier}/annotation')
    def complete(identifier: str,value: Completion):
        with db() as c:
            c.execute('BEGIN IMMEDIATE');check_version(get_history(c,identifier),value.text_hash);check_revision(c,value.schema_revision)
            if value.completed:
                require_values(c,identifier)
                for span in annotation_rows(c,identifier):
                    col=next(l for l in columns(c) if l['id']==span['label_id'])
                    if span['result_state']=='unreviewed': raise HTTPException(400,'Проверьте результат каждого фрагмента: укажите значение или выберите «Нет данных»')
                    if span['result_state']=='value': validate_value(col,span['result'])
            c.execute('INSERT INTO annotation_reviews VALUES(?,?,?) ON CONFLICT(history_id) DO UPDATE SET completed=excluded.completed,updated=excluded.updated',(identifier,int(value.completed),int(time.time())))
            c.execute('UPDATE histories SET workflow_status=? WHERE id=?',('done' if value.completed else 'in_progress',identifier))
        return {'ok':True}
    return api
