Set WshShell = CreateObject("WScript.Shell") 
WshShell.Run "cmd /c .\venv\Scripts\python.exe start_with_share.py", 0, False 
