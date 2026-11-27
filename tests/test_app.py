from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from app import create_app, connection

@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(str(tmp_path/'db'))) as c:
        yield c

def test_crud(client):
    assert client.get('/health').json() == {'status':'ok'}
    assert client.get('/ready').status_code == 200
    assert client.get('/tasks').json() == []
    r=client.post('/tasks',json={'title':' hello '}); assert r.status_code==201
    i=r.json()['id']; assert r.json()['title']=='hello'
    assert client.get(f'/tasks/{i}').json()==r.json()
    assert client.put(f'/tasks/{i}',json={'title':'new','done':True}).json()['done'] is True
    assert client.delete(f'/tasks/{i}').status_code==204
    assert client.delete(f'/tasks/{i}').status_code==404
    assert client.get(f'/tasks/{i}').status_code==404
    assert client.put(f'/tasks/{i}',json={'title':'x'}).status_code==404

@pytest.mark.parametrize('body',[{}, {'title':''},{'title':'  '},{'title':'x'*201},{'title':None},{'title':3},{'title':'x','done':1},{'title':'x','extra':1}])
def test_invalid(client,body):
    assert client.post('/tasks',json=body).status_code==422

@pytest.mark.parametrize('id',['0','-1','9223372036854775808'])
def test_bounds(client,id):
    assert client.get('/tasks/'+id).status_code==404
    assert client.put('/tasks/'+id,json={'title':'x'}).status_code==404
    assert client.delete('/tasks/'+id).status_code==404

def test_pagination_concurrency(client):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda i:client.post('/tasks',json={'title':str(i)}),range(20)))
    assert all(r.status_code==201 for r in results)
    ids=sorted(r.json()['id'] for r in results); assert len(set(ids))==20
    assert [r['id'] for r in client.get('/tasks?limit=3&offset=2').json()]==ids[2:5]
    for q in ['limit=0','limit=101','offset=-1','offset=2147483648']:
        assert client.get('/tasks?'+q).status_code==422
    assert client.post('/tasks',content='{').status_code==422

def test_persistence_failure(tmp_path):
    path=str(tmp_path/'db')
    with TestClient(create_app(path)) as c:
        r=c.post('/tasks',json={'title':"'; DROP TABLE tasks; --"}).json()
    with TestClient(create_app(path)) as c:
        assert c.get('/tasks').json()==[r]
        with connection(path) as db: db.execute('DROP TABLE tasks')
        assert c.get('/ready').status_code==503
        assert c.get('/health').status_code==200
        assert c.get('/tasks').status_code==503

def test_rollback(tmp_path):
    path=str(tmp_path/'db')
    with TestClient(create_app(path)):
        with pytest.raises(RuntimeError):
            with connection(path) as db:
                db.execute("INSERT INTO tasks(title,done) VALUES ('x',0)")
                raise RuntimeError()
        with connection(path) as db: assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==0

def test_readiness_schema_and_lock(tmp_path):
    import sqlite3
    path=str(tmp_path/'db')
    with TestClient(create_app(path)) as c:
        with connection(path) as db:
            db.execute('DROP TABLE tasks')
            db.execute('CREATE TABLE tasks (id INTEGER PRIMARY KEY)')
        assert c.get('/ready').status_code==503
        assert c.get('/health').status_code==200

def test_readiness_write_lock(tmp_path):
    path=str(tmp_path/'db')
    with TestClient(create_app(path)) as c:
        with connection(path) as db:
            db.execute('BEGIN IMMEDIATE')
            assert c.get('/ready').status_code==503
        assert c.get('/ready').status_code==200
        assert c.get('/tasks').json()==[]

def test_unicode_validation(client):
    from app import TaskInput
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        TaskInput(title=chr(0xd800))
    assert client.post('/tasks',json={'title':'Ελληνικά 🌍'}).status_code==201

@pytest.mark.parametrize('raw', [
    b'{"title":NaN}', b'{"title":Infinity}', b'{"title":"\\ud800"}',
    b'{"title":"valid","\\ud800":1}', b'{"title":"valid","done":NaN}',
])
def test_unserializable_validation_input(client, raw):
    r = client.post('/tasks', content=raw, headers={'content-type':'application/json'})
    assert r.status_code == 422
    assert isinstance(r.json()['detail'], list)
    assert client.get('/tasks').json() == []

def test_missing_database_not_recreated(tmp_path):
    path = tmp_path / 'db'
    with TestClient(create_app(str(path))) as c:
        assert c.post('/tasks', json={'title':'keep'}).status_code == 201
        path.rename(tmp_path / 'saved')
        assert c.get('/ready').status_code == 503
        assert c.get('/tasks').status_code == 503
        assert not path.exists()
        (tmp_path / 'saved').rename(path)
        assert c.get('/tasks').json()[0]['title'] == 'keep'

def test_special_path(tmp_path):
    path = tmp_path / 'db?# space'
    with TestClient(create_app(str(path))) as c:
        assert c.post('/tasks', json={'title':'ok'}).status_code == 201
        assert path.is_file()

def test_concurrent_delete_single_winner(client):
    i=client.post('/tasks',json={'title':'race'}).json()['id']
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses=list(pool.map(lambda _:client.delete(f'/tasks/{i}').status_code,range(8)))
    assert sorted(statuses)==[204]+[404]*7
    assert client.get(f'/tasks/{i}').status_code==404

def test_concurrent_replace_consistency(client):
    i=client.post('/tasks',json={'title':'initial'}).json()['id']
    bodies=[{'title':str(n),'done':bool(n%2)} for n in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda body:client.put(f'/tasks/{i}',json=body),bodies))
    for body,response in zip(bodies,results):
        assert response.status_code==200
        assert response.json()==dict(body,id=i)
    final=client.get(f'/tasks/{i}').json()
    assert final in [dict(body,id=i) for body in bodies]

def test_model_based_state(tmp_path):
    import random
    import sqlite3
    rng=random.Random(741)
    path=str(tmp_path/'model.db')
    model={}
    last_id=0
    with TestClient(create_app(path)) as c:
        for step in range(100):
            op=rng.choice(['create','replace','delete','read'])
            i=rng.randint(1,max(1,last_id+2))
            body={'title':f'task {step}','done':bool(step%2)}
            if op=='create':
                r=c.post('/tasks',json=body)
                assert r.status_code==201
                last_id+=1
                model[last_id]=dict(body,id=last_id)
                assert r.json()==model[last_id]
            elif op=='replace':
                r=c.put(f'/tasks/{i}',json=body)
                assert r.status_code==(200 if i in model else 404)
                if i in model:
                    model[i]=dict(body,id=i)
                    assert r.json()==model[i]
            elif op=='delete':
                r=c.delete(f'/tasks/{i}')
                assert r.status_code==(204 if i in model else 404)
                model.pop(i,None)
            else:
                r=c.get(f'/tasks/{i}')
                assert r.status_code==(200 if i in model else 404)
                if i in model:
                    assert r.json()==model[i]
            assert c.get('/tasks').json()==list(model.values())
        from contextlib import closing
        with closing(sqlite3.connect(path)) as db:
            rows=db.execute('SELECT id,title,done FROM tasks ORDER BY id').fetchall()
        assert rows==[(v['id'],v['title'],int(v['done'])) for v in model.values()]
