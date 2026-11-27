import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager, asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

class TaskInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    done: bool = False

    @field_validator('title')
    @classmethod
    def valid_unicode(cls, value):
        try:
            value.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise ValueError('Title must contain valid Unicode') from exc
        return value

class Task(TaskInput):
    id: int

class ErrorDetail(BaseModel):
    detail: str

class ValidationIssue(BaseModel):
    loc: list[str | int]
    type: str
    msg: str

class ValidationFailure(BaseModel):
    detail: list[ValidationIssue]

DB_ERROR = {503: {'model': ErrorDetail}}
VALIDATION = {422: {'model': ValidationFailure}}
NOT_FOUND = {404: {'model': ErrorDetail}}

@contextmanager
def connection(path, *, initialize=False):
    uri = Path(path).resolve().as_uri() + ('?mode=rwc' if initialize else '?mode=rw')
    db = sqlite3.connect(uri, uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()

def create_app(path=None):
    path = str(Path(path or os.environ.get('TASK_DB', 'tasks.sqlite3')).resolve())
    @asynccontextmanager
    async def lifespan(app):
        with connection(path, initialize=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, done INTEGER NOT NULL CHECK(done IN (0,1)))')
        yield
    api = FastAPI(title='Task Service', lifespan=lifespan)
    def task(row):
        if row is None:
            raise HTTPException(404, 'Task not found')
        return Task(id=row['id'], title=row['title'], done=bool(row['done']))
    @api.get('/health')
    def health():
        return {'status': 'ok'}
    @api.get('/ready', responses=DB_ERROR)
    def ready():
        try:
            with connection(path) as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('SELECT id, title, done FROM tasks LIMIT 1').fetchone()
                db.execute('UPDATE tasks SET done=done WHERE 0')
                db.rollback()
        except sqlite3.Error as exc:
            raise HTTPException(503, 'Database unavailable') from exc
        return {'status': 'ready'}
    @api.post('/tasks', response_model=Task, status_code=201, responses=DB_ERROR | VALIDATION)
    def create(body: TaskInput):
        with connection(path) as db:
            cursor = db.execute('INSERT INTO tasks(title,done) VALUES (?,?)', (body.title, body.done))
            return task(db.execute('SELECT * FROM tasks WHERE id=?', (cursor.lastrowid,)).fetchone())
    @api.get('/tasks', response_model=list[Task], responses=DB_ERROR | VALIDATION)
    def listing(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=2147483647)):
        with connection(path) as db:
            return [task(r) for r in db.execute('SELECT * FROM tasks ORDER BY id LIMIT ? OFFSET ?', (limit, offset))]
    @api.get('/tasks/{id}', response_model=Task, responses=DB_ERROR | VALIDATION | NOT_FOUND)
    def get(id: int):
        with connection(path) as db:
            return task(db.execute('SELECT * FROM tasks WHERE id=?', (id,)).fetchone()) if 0 < id <= 9223372036854775807 else task(None)
    @api.put('/tasks/{id}', response_model=Task, responses=DB_ERROR | VALIDATION | NOT_FOUND)
    def update(id: int, body: TaskInput):
        if not 0 < id <= 9223372036854775807:
            return task(None)
        with connection(path) as db:
            db.execute('UPDATE tasks SET title=?, done=? WHERE id=?', (body.title, body.done, id))
            return task(db.execute('SELECT * FROM tasks WHERE id=?', (id,)).fetchone())
    @api.delete('/tasks/{id}', status_code=204, responses=DB_ERROR | VALIDATION | NOT_FOUND)
    def delete(id: int):
        if not 0 < id <= 9223372036854775807:
            raise HTTPException(404, 'Task not found')
        with connection(path) as db:
            if not db.execute('DELETE FROM tasks WHERE id=?', (id,)).rowcount:
                raise HTTPException(404, 'Task not found')
    @api.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Do not echo arbitrary input: it can contain NaN or lone surrogates.
        errors = []
        for error in exc.errors():
            loc = [part.encode('utf-8', 'backslashreplace').decode('utf-8') if isinstance(part, str) else part for part in error['loc']]
            errors.append({'loc': loc, 'type': error['type'], 'msg': error['msg']})
        return JSONResponse(status_code=422, content={'detail': errors})
    @api.exception_handler(sqlite3.Error)
    async def database_error(request, exc):
        return JSONResponse(status_code=503, content={'detail': 'Database unavailable'})
    return api

app = create_app()
