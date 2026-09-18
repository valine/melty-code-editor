@echo off
rem Launcher: run editor.py on the project's own venv (.venv beside this file),
rem with meltygui and meltygui_pro installed editable - see README "Setup".
set "here=%~dp0"
if not exist "%here%.venv\Scripts\python.exe" (
    echo melty-code-editor: no .venv - run the Setup steps in README.md 1>&2
    exit /b 1
)
"%here%.venv\Scripts\python.exe" "%here%editor.py" %*
