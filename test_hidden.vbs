Set WshShell = CreateObject("WScript.Shell")  
WshShell.Run "cmd /c .\venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000", 0, False  
