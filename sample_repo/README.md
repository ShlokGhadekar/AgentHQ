# Notes API

A small FastAPI service for personal notes: register, log in for a bearer token, then create,
list, read and delete your own notes. Data is kept in memory.

This repository is the target codebase for [AgentHQ](https://github.com/ShlokGhadekar/AgentHQ)'s
agent team and evaluation tasks.

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
pytest -q
```

## Endpoints

| Method | Path          | Auth   | Description                        |
|--------|---------------|--------|------------------------------------|
| POST   | /users        | no     | Register `{username, password}`    |
| POST   | /login        | no     | Get a bearer token                 |
| GET    | /notes        | bearer | List your notes                    |
| POST   | /notes        | bearer | Create `{title, body}`             |
| GET    | /notes/{id}   | bearer | Read one of your notes             |
| DELETE | /notes/{id}   | bearer | Delete one of your notes           |
