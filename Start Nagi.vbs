Option Explicit
Dim shell, files, root, command, arg
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & root & "\launch-nagi.ps1"""
If WScript.Arguments.Count > 0 Then
  arg = WScript.Arguments(0)
  If arg = "stop" Then command = command & " -Stop"
  If arg = "data" Then command = command & " -ChooseDataDirectory"
End If
shell.Run command, 0, False
