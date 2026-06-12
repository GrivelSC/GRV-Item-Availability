@echo off
REM ===========================================================================
REM GRV daily automation launcher (called by Windows Task Scheduler)
REM ===========================================================================
cd /d C:\GRV-Availability

REM If the scheduled task fails with "python is not recognized", it means the
REM Task Scheduler service context does not have Python on PATH. Fix: run
REM   where python
REM in a normal Command Prompt, then replace the word python below with the
REM full path it prints, in quotes, e.g.:
REM   "C:\Users\you\AppData\Local\Programs\Python\Python312\python.exe" run_daily.py > ...
python run_daily.py > "C:\GRV-Availability\run_daily.lastrun.txt" 2>&1
