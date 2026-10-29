@echo off
chcp 65001 >nul
echo Starting AI Discussant Studio (GPT 6 - Astra)...
start "" "http://127.0.0.1:8080"
python "%~dp0server.py"
pause
