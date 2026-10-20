@echo off
cd /d D:\zidongh\backend
set DATABASE_URL=sqlite:///./naw.db
.venv\Scripts\python.exe -c "from app.main import app; print('app loaded, routes=', len(app.routes))"
