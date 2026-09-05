' ===================================================================
'  業務ツール統合ランチャー 通常起動
'
'  **利用者はこの1つだけをダブルクリックしてください。**
'  コンソールを出さずに起動するので、画面下に細長いランチャーバーが
'  出てくるだけになります(要件定義書 §17 / 基盤仕様書 2.1)。
'
'  起動しないときは start_debug.bat を使うと理由が表示されます。
'
'  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
'  WSH reads a .vbs with the system ANSI code page, which is 932 on the
'  Japanese Windows this tool runs on. tests/test_launch_files.py checks it.
' ===================================================================
Option Explicit

Const APP_NAME = "業務ツール統合ランチャー"

Dim shell, fso, here, script, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(here, "launcher.py")

' 本体が同じフォルダーにあるか。**pythonw はコンソールを出さない**ので、
' このまま起動すると本当に「何も起きない」ように見えます
' (このファイルだけをデスクトップにコピーすると、この状態になります)
If Not fso.FileExists(script) Then
    MsgBox "launcher.py が見つかりません。" & vbCrLf & vbCrLf & _
           "探した場所: " & script & vbCrLf & vbCrLf & _
           "このファイルは、ランチャー一式が入ったフォルダーの中から" & vbCrLf & _
           "実行してください(ショートカットを作るのは大丈夫です)。", _
           vbCritical, APP_NAME
    WScript.Quit 1
End If

' Python があるか先に確かめる。無いまま起動すると、
' コンソールが出ない分だけ「何も起きない」ように見えてしまう
If shell.Run("cmd /c python --version", 0, True) <> 0 Then
    MsgBox "Python が見つかりません。" & vbCrLf & vbCrLf & _
           "https://www.python.org/downloads/ からインストールし、" & vbCrLf & _
           "インストールの最初の画面で「Add python.exe to PATH」に" & vbCrLf & _
           "チェックを入れてください。" & vbCrLf & vbCrLf & _
           "詳しい理由を見るには start_debug.bat を実行してください。", _
           vbCritical, APP_NAME
    WScript.Quit 1
End If

' 実際に使うのは pythonw のほう。python はあるのに pythonw が無い場合が
' あるので、走らせる前に確かめる(黙って飛ばすと無言で失敗する)
If shell.Run("cmd /c pythonw --version", 0, True) <> 0 Then
    MsgBox "pythonw が見つかりません。" & vbCrLf & vbCrLf & _
           "Python は入っていますが、画面を出さずに起動するための" & vbCrLf & _
           "pythonw.exe がありません。" & vbCrLf & _
           "start_debug.bat から起動してください(コンソールが開きます)。", _
           vbCritical, APP_NAME
    WScript.Quit 1
End If

' pythonw はコンソールを出さない。ランチャーは常駐するので
' 待たずに抜ける(False)。失敗の通知は launcher.py がダイアログで行う。
' パスは**絶対パスで渡す**。共有フォルダーから実行されることがあり、
' 作業フォルダーが違うと見つからないことがある
shell.CurrentDirectory = here
cmd = "pythonw " & Chr(34) & script & Chr(34)
shell.Run cmd, 0, False
