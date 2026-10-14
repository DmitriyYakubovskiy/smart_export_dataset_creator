import json
import sqlite3

import pytest
from test_app import client, upload, main
from app import annotations


def defaults(client):
    return {l['id']:(0 if l['data_type']=='int' else False if l['data_type']=='bool' else l['options'][0]) for l in client.get('/api/labels').json() if l['active']}


def history(client,text='Пациент 🚬 не курит. Ранее курил. Не курит.'):
    document_id=upload(client)
    hid=client.post(f'/api/documents/{document_id}/histories',json={'text':text,'values':defaults(client)}).json()['id']
    return client.get(f'/api/histories/{hid}/annotation').json()


def span(client,h,fragment='не курит',label=None,start=None):
    label=label or next(l['id'] for l in client.get('/api/labels').json() if l['code']=='SMOKING_MENTION')
    start=h['text'].index(fragment) if start is None else start
    return {'label_id':label,'start':start,'end':start+len(fragment),'text':fragment,'text_hash':h['text_hash'],'result':next(l['options'][0] if l['data_type']=='enum' else False if l['data_type']=='bool' else 0 for l in client.get('/api/labels').json() if l['id']==label)}


def test_unicode_offsets_and_export(client):
    h=history(client)
    value=span(client,h)
    r=client.post(f"/api/histories/{h['id']}/annotations",json=value)
    assert r.status_code==201, r.text
    detail=client.get(f"/api/histories/{h['id']}/annotation").json()
    a=detail['annotations'][0]
    assert a['label']=='SMOKING_MENTION'
    assert a['start']==10 and h['text'][a['start']:a['end']]=='не курит'
    exported=next(r for r in client.get('/api/export/json').json() if r['id']==h['id'])
    assert exported['annotations'][0]['start']==10
    assert exported['offset_unit']=='unicode_codepoint' and exported['end_exclusive'] is True
    assert exported['document_name']=='история.docx'


def test_repeated_mentions_are_distinct(client):
    h=history(client,'не курит; не курит')
    for start in (0,10):
        value=span(client,h,start=start)
        assert client.post(f"/api/histories/{h['id']}/annotations",json=value).status_code==201
    assert len(client.get(f"/api/histories/{h['id']}/annotation").json()['annotations'])==2


def test_custom_label_and_overlap(client):
    h=history(client)
    response=client.post('/api/labels',json={'code':'CUSTOM_TEST','name':'Произвольный параметр','color':'#abcdef'})
    assert response.status_code==201
    label=response.json()['id']
    for value in (span(client,h),span(client,h,fragment='курит',label=label)):
        assert client.post(f"/api/histories/{h['id']}/annotations",json=value).status_code==201
    items=client.get(f"/api/histories/{h['id']}/annotation").json()['annotations']
    assert {a['label'] for a in items}=={'CUSTOM_TEST','SMOKING_MENTION'}
    assert client.patch('/api/labels/'+label,json={'name':'Новое имя','color':'#112233','active':False}).status_code==200
    assert client.post(f"/api/histories/{h['id']}/annotations",json=span(client,h,label=label)).status_code==400
    assert len(client.get(f"/api/histories/{h['id']}/annotation").json()['annotations'])==2


@pytest.mark.parametrize('changes,expected',[
    ({'start':-1},422),({'start':True},422),({'end':99999},400),
    ({'end':1},400),({'text':'другой текст'},400),
    ({'text_hash':'stale'},409),({'label_id':'missing'},400),
],ids=['negative','boolean','beyond_text','reversed','wrong_fragment','stale','missing_label'])
def test_invalid_span(client,changes,expected):
    h=history(client)
    assert client.post(f"/api/histories/{h['id']}/annotations",json={**span(client,h),**changes}).status_code==expected
    assert not client.get(f"/api/histories/{h['id']}/annotation").json()['annotations']


def test_duplicate_and_safe_text_edit(client):
    h=history(client);path=f"/api/histories/{h['id']}"
    value=span(client,h)
    assert client.post(path+'/annotations',json=value).status_code==201
    assert client.post(path+'/annotations',json=value).status_code==409
    assert client.put(path,json={'text':'Изменённый текст'}).status_code==409
    assert client.put(path,json={'text':h['text'],'title':'Новое название'}).status_code==200
    assert client.post(path+'/annotations/reset',json={'text_hash':h['text_hash']}).status_code==200
    assert client.put(path,json={'text':'Изменённый текст'}).status_code==200
    assert client.post(path+'/annotations',json=value).status_code==409


def test_completion_negative_examples_and_reopening(client):
    h=history(client,'Сведения отсутствуют.');path=f"/api/histories/{h['id']}"
    assert client.patch(path+'/annotation',json={'text_hash':h['text_hash'],'completed':True,'schema_revision':client.get('/api/schema').json()['revision']}).status_code==200
    done=client.get('/api/histories?status=completed').json()
    assert any(r['id']==h['id'] and r['annotation_count']==0 for r in done)
    assert not any(r['id']==h['id'] for r in client.get('/api/histories?status=pending').json())
    exported=client.get('/api/export/json?status=completed').json()
    assert any(r['id']==h['id'] and r['annotations']==[] for r in exported)
    value=span(client,h,fragment='отсутствуют')
    added=client.post(path+'/annotations',json=value)
    assert added.status_code==201
    assert not client.get(path+'/annotation').json()['completed']
    client.patch(path+'/annotation',json={'text_hash':h['text_hash'],'completed':True,'schema_revision':client.get('/api/schema').json()['revision']})
    assert client.delete(path+'/annotations/'+added.json()['id']).status_code==200
    assert not client.get(path+'/annotation').json()['completed']


def test_delete_history_cascades(client):
    h=history(client);path=f"/api/histories/{h['id']}"
    client.post(path+'/annotations',json=span(client,h))
    assert client.delete(path).status_code==200
    with main.db() as c:
        assert not c.execute('SELECT 1 FROM annotations WHERE history_id=?',(h['id'],)).fetchone()
        assert not c.execute('SELECT 1 FROM annotation_reviews WHERE history_id=?',(h['id'],)).fetchone()


def test_seed_is_idempotent_and_preserves_user_changes():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE histories(id TEXT PRIMARY KEY,text TEXT)')
    c.execute("INSERT INTO histories VALUES('existing','Старый анамнез')")
    annotations.initialize(c)
    c.execute("UPDATE labels SET name='Моё название',active=0 WHERE code='SMOKING_MENTION'")
    annotations.initialize(c)
    assert c.execute('SELECT COUNT(*) FROM labels').fetchone()[0]==1
    assert c.execute('SELECT name,active FROM labels').fetchone()[:]==('Моё название',0)
    assert c.execute('SELECT text FROM histories').fetchone()[0]=='Старый анамнез'
    c.close()


def test_label_validation_and_access(client):
    from fastapi.testclient import TestClient
    anonymous=TestClient(main.app)
    assert anonymous.get('/api/labels').status_code==401
    assert anonymous.get('/api/histories').status_code==401
    assert client.post('/api/labels',json={'code':'bad code','name':'bad'}).status_code==422
    assert client.post('/api/labels',json={'code':'SMOKING_MENTION','name':'Повтор'}).status_code==409
