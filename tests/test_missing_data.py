"""Explicit null is checked absence; legacy empty fields remain unreviewed."""
import csv
import io
import json

import pytest
from test_app import client, main
from test_annotations import history, span
from test_workflow import get, complete, rev
from app.annotations import initialize


@pytest.mark.parametrize('kind,real', [('bool', False), ('int', 0), ('enum', 'неизвестно')])
def test_no_data_roundtrip_and_completion(client, kind, real):
    col=client.post('/api/labels',json={'code':'MISSING_'+kind.upper(),'name':kind,'data_type':kind,'options':['неизвестно']}).json()['id']
    h=history(client)
    path='/api/histories/'+h['id']
    def save(value,state=None):
        payload={'title':h['title'],'text':h['text'],'values':{col:value}}
        if state: payload['value_states']={col:state}
        return client.put(path,json=payload)
    # Old clients still clear fields with null, and completion is blocked.
    assert save(None).status_code==200
    assert get(client,h)['value_states'][col]=='unreviewed'
    assert col not in get(client,h)['values']
    assert complete(client,h).status_code==400
    assert save(None,'no_data').status_code==200
    current=get(client,h)
    assert current['values'][col] is None and current['value_states'][col]=='no_data'
    assert complete(client,h).status_code==200  # No spans needed.
    assert client.patch('/api/documents/'+h['document_id']+'/status',json={'status':'done','schema_revision':rev(client)}).status_code==200
    assert client.patch('/api/documents/'+h['document_id'],json={'reviewed':True}).status_code==200
    for rows in [client.get('/api/export/json').json(),list(csv.DictReader(io.StringIO(client.get('/api/export/csv').content.decode('utf-8-sig')),delimiter=';'))]:
        row=next(r for r in rows if r['id']==h['id'])
        cols=json.loads(row['columns']) if isinstance(row['columns'],str) else row['columns']
        exported=next(c for c in cols if c['label_id']==col)
        assert exported['value'] is None and exported['state']=='no_data'
    listed=next(r for r in client.get('/api/documents/'+h['document_id']+'/histories').json() if r['id']==h['id'])
    assert listed['values'][col] is None and listed['value_states'][col]=='no_data'
    # Idempotent startup leaves completed records alone.
    with main.db() as c: initialize(c);initialize(c)
    assert get(client,h)['completed'] and get(client,h)['value_states'][col]=='no_data'
    # Saving unchanged form does not reopen a completed record.
    assert save(None,'no_data').status_code==200
    assert get(client,h)['completed']
    assert save(real,'value').status_code==200
    assert get(client,h)['values'][col]==real
    assert not get(client,h)['completed']
    assert save(None,'no_data').status_code==200
    assert complete(client,h).status_code==200
    assert save(None,'unreviewed').status_code==200
    assert get(client,h)['value_states'][col]=='unreviewed'
    assert not get(client,h)['completed']
    assert complete(client,h).status_code==400
    for state,value in [('no_data',real),('unreviewed',real),('value',None)]:
        assert save(value,state).status_code==400
    assert save(None,'typo').status_code==422


@pytest.mark.parametrize('kind', ['bool','int','enum'])
def test_span_explicit_null_vs_legacy_null(client,kind):
    col=client.post('/api/labels',json={'code':'SPAN_NULL_'+kind.upper(),'name':kind,'data_type':kind,'options':['упоминание']}).json()['id']
    h=history(client)
    path='/api/histories/'+h['id']+'/annotations'
    payload={**span(client,h,label=col),'result':None,'result_state':'no_data'}
    response=client.post(path,json=payload)
    assert response.status_code==201,response.text
    aid=response.json()['id']
    current=get(client,h)
    assert current['annotations'][0]['result'] is None
    assert current['annotations'][0]['result_state']=='no_data'
    assert complete(client,h).status_code==200
    # Previous releases left SQL NULL for unchecked span results.
    with main.db() as c:
        c.execute('UPDATE annotations SET result=NULL WHERE id=?',(aid,))
        initialize(c)
    assert get(client,h)['annotations'][0]['result_state']=='unreviewed'
    assert complete(client,h).status_code==400
    assert client.patch(path+'/'+aid,json=payload).status_code==200
    assert complete(client,h).status_code==200
    payload['result_state']='unreviewed'
    assert client.patch(path+'/'+aid,json=payload).status_code==200
    assert complete(client,h).status_code==400
    payload.pop('result_state')
    assert client.patch(path+'/'+aid,json=payload).status_code==400
    payload['result_state']='no_data';payload['result']=True
    assert client.patch(path+'/'+aid,json=payload).status_code==400
