@echo off
python -m venv venv
call venv\Scripts\activate
pip install -r requirements.txt
set ADMIN_USERNAME=admin
set ADMIN_PASSWORD=ChangeMe123!
set SECRET_KEY=local-dev-only
set COOKIE_SECURE=false
set FLASK_DEBUG=1
python app.py
pause
