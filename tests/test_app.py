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
