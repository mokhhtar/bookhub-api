@echo off
cd /d "%~dp0"
start "" http://localhost:4590
node server.js
