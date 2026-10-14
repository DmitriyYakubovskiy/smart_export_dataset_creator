import sqlite3

import pytest
from test_app import client, main
from test_annotations import history, span, defaults
from app.annotations import initialize


def rev(c): return c.get('/api/schema').json()['revision']
def get(c,h): return c.get(f"/api/histories/{h['id']}/annotation").json()
def doc(c,h): return next(d for d in c.get('/api/documents').json() if d['id']==h['document_id'])
def complete(c,h):
    h=get(c,h)
    return c.patch(f"/api/histories/{h['id']}/annotation",json={'completed':True,'text_hash':h['text_hash'],'schema_revision':rev(c)})


def test_two_stage_workflow_and_schema_invalidation(client):
    h=history(client);did=h['document_id'];path=f'/api/documents/{did}'
    assert client.post(path+'/open').status_code==200
    assert doc(client,h)['workflow_status']=='in_progress'
    assert client.patch(path+'/status',json={'status':'done','schema_revision':rev(client)}).status_code==200
    assert client.post(path+'/open').status_code==200
    assert doc(client,h)['workflow_status']=='done'
    assert client.post(f"/api/histories/{h['id']}/open").status_code==200
    assert get(client,h)['workflow_status']=='in_progress'
    response=client.post(f"/api/histories/{h['id']}/annotations",json=span(client,h))
    assert response.status_code==201
    assert complete(client,h).status_code==200
    assert get(client,h)['workflow_status']=='done'
    before=rev(client)
    new=client.post('/api/labels',json={'code':'WORKFLOW_NEW','name':'Новый параметр','data_type':'bool'})
    assert new.status_code==201
    assert rev(client)==before+1
    assert doc(client,h)['workflow_status']=='in_progress'
    current=get(client,h)
    assert current['workflow_status']=='in_progress' and not current['completed']
    assert len(current['annotations'])==1  # No destructive reset of labels or text.
    assert client.patch(path+'/status',json={'status':'done','schema_revision':before}).status_code==409
    assert complete(client,h).status_code==400
    assert client.put('/api/histories/'+h['id'],json={'text':h['text'],'values':{new.json()['id']:False}}).status_code==200
    assert complete(client,h).status_code==200
    assert client.patch(path+'/status',json={'status':'done','schema_revision':rev(client)}).status_code==200


@pytest.mark.parametrize('kind,value,expected',[
    ('int',0,200),('int',True,400),('int',1.5,400),('int','1',400),
    ('bool',False,200),('bool',0,400),('bool','нет',400),
    ('enum','не курит',200),('enum','выдумано',400),
],ids=['int_zero','int_bool','int_fraction','int_string','bool_false','bool_int','bool_string','enum_valid','enum_invalid'])
def test_typed_history_values(client,kind,value,expected):
    code='TYPED_'+kind.upper()
    labels=client.get('/api/labels').json();column=next((l for l in labels if l['code']==code),None)
    if column is None:
        result=client.post('/api/labels',json={'code':code,'name':kind,'data_type':kind,'options':['курит','не курит']})
        column={'id':result.json()['id']}
    h=history(client)
    r=client.put('/api/histories/'+h['id'],json={'text':h['text'],'values':{column['id']:value}})
    assert r.status_code==expected,r.text
    if expected==200: assert get(client,h)['values'][column['id']] == value


def test_span_result_and_explicit_source_ids(client):
    h=history(client);value=span(client,h);value['result']='не курит'
    added=client.post(f"/api/histories/{h['id']}/annotations",json=value)
    assert added.status_code==201
    a=get(client,h)['annotations'][0]
    assert a['history_id']==h['id'] and a['document_id']==h['document_id']
    assert a['result']=='не курит'
    value['result']='курит'
    assert client.patch(f"/api/histories/{h['id']}/annotations/{a['id']}",json=value).status_code==200
    exported=next(r for r in client.get('/api/export/json').json() if r['id']==h['id'])
    assert exported['annotations'][0]['result']=='курит'
    assert exported['document_id']==h['document_id']
    assert exported['columns'] and exported['schema_revision']==rev(client)
    value['result']='unknown-value'
    assert client.patch(f"/api/histories/{h['id']}/annotations/{a['id']}",json=value).status_code==400


def test_missing_values_block_document_completion(client):
    h=history(client)
    col=next(l for l in client.get('/api/labels').json() if l['active'])
    client.put('/api/histories/'+h['id'],json={'text':h['text'],'values':{col['id']:None}})
    r=client.patch(f"/api/documents/{h['document_id']}/status",json={'status':'done','schema_revision':rev(client)})
    assert r.status_code==400


def test_restart_does_not_invalidate_or_duplicate_columns(client):
    before=rev(client);count=len(client.get('/api/labels').json())
    with main.db() as c: initialize(c)
    assert rev(client)==before and len(client.get('/api/labels').json())==count


def test_upgrade_keeps_legacy_spans_and_links():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    c.executescript('''CREATE TABLE documents(id TEXT PRIMARY KEY,reviewed INTEGER);
      CREATE TABLE histories(id TEXT PRIMARY KEY,document_id TEXT,text TEXT);
      CREATE TABLE labels(id TEXT PRIMARY KEY,code TEXT UNIQUE,name TEXT,color TEXT,active INTEGER DEFAULT 1);
      CREATE TABLE annotations(id TEXT PRIMARY KEY,history_id TEXT,label_id TEXT,start INTEGER,end INTEGER,text TEXT,created INTEGER);
      CREATE TABLE annotation_reviews(history_id TEXT PRIMARY KEY,completed INTEGER,updated INTEGER);
      INSERT INTO documents VALUES('d',1);
      INSERT INTO histories VALUES('h','d','Не курит');
      INSERT INTO labels VALUES('l','SMOKING_MENTION','Курение','#f5cc76',1);
      INSERT INTO annotations VALUES('a','h','l',0,8,'Не курит',1);
      INSERT INTO annotation_reviews VALUES('h',1,1);''')
    initialize(c)
    row=c.execute('SELECT * FROM annotations').fetchone()
    assert row['history_id']=='h' and row['text']=='Не курит' and row['result'] is None
    assert c.execute('SELECT completed FROM annotation_reviews').fetchone()[0]==0
    assert c.execute('SELECT document_id FROM histories').fetchone()[0]=='d'
    assert c.execute('SELECT data_type FROM labels').fetchone()[0]=='enum'
    c.close()
