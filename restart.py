"""One-shot restart helper — kills ATS processes and starts fresh."""
import os
import subprocess
import sys
import time

ps = r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'

kill_cmd = (
    'Get-WmiObject Win32_Process -Filter "Name=\'python.exe\'" '
    '| Where-Object { $_.CommandLine -like "*xau_ats*" '
    '-or $_.CommandLine -like "*main.py*" '
    '-or $_.CommandLine -like "*telegram_bot*" } '
    '| Where-Object { $_.ProcessId -ne ' + str(os.getpid()) + ' } '
    '| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }'
)
subprocess.run([ps, '-NoProfile', '-Command', kill_cmd], capture_output=True)
print("Killed old ATS processes")
time.sleep(2)

for f in [r'd:\xau_ats\logs\runner.lock', r'd:\xau_ats\logs\telegram_bot.lock']:
    try:
        os.remove(f)
        print("Removed", f)
    except FileNotFoundError:
        pass

venv_python = r'd:\xau_ats\.venv\Scripts\python.exe'
proc = subprocess.Popen(
    [venv_python, 'start.py'],
    cwd=r'd:\xau_ats',
    stdout=open(r'd:\xau_ats\logs\start_stdout.log', 'w'),
    stderr=subprocess.STDOUT,
    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
)
print(f"Started PID {proc.pid}")
