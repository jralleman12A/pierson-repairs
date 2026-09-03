@echo off
python -m venv venv
call venv\Scripts\activate
pip install -r requirements.txt
set ADMIN_PASSWORD=ChangeMe123!
python app.py
pause
