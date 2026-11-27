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

@pytest.mark.parametrize('operation', ['INSERT','UPDATE','DELETE'])
def test_trigger_failure_atomicity(tmp_path, operation):
    path=str(tmp_path/'fault.db')
    with TestClient(create_app(path)) as c:
        original=c.post('/tasks',json={'title':'original'}).json()
        i=original['id']
        with connection(path) as db:
            db.execute(f"CREATE TRIGGER injected BEFORE {operation} ON tasks BEGIN SELECT RAISE(ABORT, 'injected'); END")
        def mutate():
            if operation=='INSERT':
                return c.post('/tasks',json={'title':'new'})
            if operation=='UPDATE':
                return c.put(f'/tasks/{i}',json={'title':'changed','done':True})
            return c.delete(f'/tasks/{i}')
        r=mutate()
        assert r.status_code==503
        assert r.json()=={'detail':'Database unavailable'}
        assert c.get('/tasks').json()==[original]
        assert c.get('/health').status_code==200
        with connection(path) as db:
            db.execute('DROP TRIGGER injected')
        assert mutate().status_code=={'INSERT':201,'UPDATE':200,'DELETE':204}[operation]

def test_commit_failure_rolls_back(tmp_path):
    import sqlite3
    path=str(tmp_path/'commit.db')
    with TestClient(create_app(path)) as c:
        original=c.post('/tasks',json={'title':'original'}).json()
        with connection(path) as db:
            db.execute('CREATE TABLE parent (id INTEGER PRIMARY KEY)')
            db.execute('CREATE TABLE child (id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)')
        with pytest.raises(sqlite3.IntegrityError):
            with connection(path) as db:
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('UPDATE tasks SET title=?',('changed',))
                db.execute('INSERT INTO child VALUES (1)')
        assert c.get('/tasks').json()==[original]
        with connection(path) as db:
            assert db.execute('SELECT COUNT(*) FROM child').fetchone()[0]==0
        assert c.put(f"/tasks/{original['id']}",json={'title':'recovered'}).status_code==200

def test_openapi_error_contract(client):
    from app import ValidationFailure, ErrorDetail
    schema=client.get('/openapi.json').json()
    for path,method,codes in [
        ('/ready','get',['503']),
        ('/tasks','post',['422','503']),
        ('/tasks','get',['422','503']),
        *[('/tasks/{id}',m,['404','422','503']) for m in ['get','put','delete']],
    ]:
        responses=schema['paths'][path][method]['responses']
        for code in codes:
            expected='ValidationFailure' if code=='422' else 'ErrorDetail'
            assert code in responses, (path, method, code)
            assert responses[code]['content']['application/json']['schema']['$ref']=='#/components/schemas/'+expected
    ValidationFailure.model_validate(client.post('/tasks',json={}).json())
    ErrorDetail.model_validate(client.get('/tasks/999').json())
    assert 'content' not in schema['paths']['/tasks/{id}']['delete']['responses']['204']

def test_put_error_response_contract(tmp_path):
    from app import ErrorDetail, ValidationFailure
    path=str(tmp_path/'contract.db')
    with TestClient(create_app(path)) as c:
        r=c.put('/tasks/999',json={'title':'valid'})
        assert r.status_code==404
        ErrorDetail.model_validate(r.json())
        r=c.put('/tasks/999',json={})
        assert r.status_code==422
        ValidationFailure.model_validate(r.json())
        with connection(path) as db:
            db.execute('DROP TABLE tasks')
        r=c.put('/tasks/999',json={'title':'valid'})
        assert r.status_code==503
        ErrorDetail.model_validate(r.json())

@pytest.mark.parametrize('title,done', [('valid',2), ('  padded  ',0), ('',0), ('x'*201,0)])
def test_corrupt_stored_task(tmp_path,title,done):
    path=str(tmp_path/'corrupt.db')
    with TestClient(create_app(path)) as c:
        with connection(path) as db:
            db.execute('PRAGMA ignore_check_constraints=ON')
            db.execute('INSERT INTO tasks(title,done) VALUES (?,?)',(title,done))
        assert c.get('/tasks/1').status_code==503
        assert c.get('/tasks').status_code==503
        assert c.get('/health').status_code==200
        assert c.put('/tasks/1',json={'title':'repaired'}).status_code==200
        assert c.get('/tasks/1').json()['title']=='repaired'

def test_invalid_id_independent_of_database(tmp_path):
    path=tmp_path/'db'
    with TestClient(create_app(str(path))) as c:
        path.unlink()
        for method in ('get','put','delete'):
            kwargs={'json':{'title':'valid'}} if method=='put' else {}
            assert getattr(c,method)('/tasks/0',**kwargs).status_code==404
        assert c.get('/tasks/1').status_code==503

def test_missing_stored_column(tmp_path):
    path=str(tmp_path/'schema.db')
    with TestClient(create_app(path)) as c:
        with connection(path) as db:
            db.execute('ALTER TABLE tasks RENAME COLUMN done TO old_done')
            db.execute("INSERT INTO tasks(title,old_done) VALUES ('existing',0)")
        assert c.get('/tasks/1').status_code==503
        assert c.get('/tasks').status_code==503
        assert c.get('/ready').status_code==503

def test_ignored_update_not_reported_success(tmp_path):
    path=str(tmp_path/'ignored.db')
    with TestClient(create_app(path)) as c:
        original=c.post('/tasks',json={'title':'original'}).json()
        with connection(path) as db:
            db.execute("CREATE TRIGGER ignored BEFORE UPDATE ON tasks BEGIN SELECT RAISE(IGNORE); END")
        r=c.put('/tasks/1',json={'title':'changed','done':True})
        assert r.status_code==503
        assert c.get('/tasks/1').json()==original
        assert c.put('/tasks/999',json={'title':'missing'}).status_code==404
        with connection(path) as db:
            db.execute('DROP TRIGGER ignored')
        assert c.put('/tasks/1',json={'title':'changed'}).status_code==200

@pytest.mark.parametrize('trigger', [
    "CREATE TRIGGER fault BEFORE INSERT ON tasks BEGIN SELECT RAISE(IGNORE); END",
    "CREATE TRIGGER fault AFTER INSERT ON tasks BEGIN DELETE FROM tasks WHERE id=NEW.id; END",
    "CREATE TRIGGER fault AFTER INSERT ON tasks BEGIN UPDATE tasks SET title='wrong' WHERE id=NEW.id; END",
])
def test_insert_postcondition(tmp_path, trigger):
    path=str(tmp_path/'postcondition.db')
    with TestClient(create_app(path)) as c:
        original=c.post('/tasks',json={'title':'original'}).json()
        with connection(path) as db:
            db.execute(trigger)
        assert c.post('/tasks',json={'title':'requested'}).status_code==503
        assert c.get('/tasks').json()==[original]
        with connection(path) as db:
            db.execute('DROP TRIGGER fault')
        assert c.post('/tasks',json={'title':'requested'}).status_code==201

@pytest.mark.parametrize('action', [
    "DELETE FROM tasks WHERE id=NEW.id",
    "UPDATE tasks SET title='wrong' WHERE id=NEW.id",
])
def test_update_postcondition(tmp_path, action):
    path=str(tmp_path/'postcondition.db')
    with TestClient(create_app(path)) as c:
        original=c.post('/tasks',json={'title':'original'}).json()
        with connection(path) as db:
            db.execute(f'CREATE TRIGGER fault AFTER UPDATE ON tasks BEGIN {action}; END')
        assert c.put('/tasks/1',json={'title':'requested'}).status_code==503
        assert c.get('/tasks/1').json()==original
