@echo off
chcp 65001 >nul

:: 画面を即座にオフにするためのPowerShellコマンド
powershell.exe -Command "$HWND=-1; $WM_SYSCOMMAND=0x0112; $SC_MONITORPOWER=0xF170; $MONITOR_OFF=2; Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class Win32 { [DllImport(\"user32.dll\")] public static extern int SendMessage(int hWnd, int hMsg, int wParam, int lParam); }'; [Win32]::SendMessage($HWND, $WM_SYSCOMMAND, $SC_MONITORPOWER, $MONITOR_OFF)"

exit
