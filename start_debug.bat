@echo off
rem  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
rem  Everything above the chcp line must stay ASCII. cmd.exe parses a .bat
rem  with the *console* code page, so the page has to be set before the
rem  first non-ASCII byte - including the bytes in these comments.
rem  In CP932 a trail byte can be 0x5C or 0x40, so some characters turn
rem  into a literal backslash or at-sign under a different code page.
rem  tests/test_launch_files.py enforces the encoding and this ordering.
chcp 932 >nul 2>&1
rem ===================================================================
rem  業務ツール統合ランチャー 診断起動 (要件定義書 §17)
rem
rem  普段は Start.vbs を使ってください。こちらは「起動しないとき」に
rem  理由を見るためのもので、コンソールを開いたまま経過を表示します。
rem
rem  よく使うのは次の2つ:
rem      start_debug.bat            確認してから起動する
rem      start_debug.bat --check    確認だけして終わる
rem ===================================================================
setlocal

rem  共有フォルダー(\\サーバ\...)は現在地にできないので pushd を使う。
rem  pushd は一時的にドライブ文字を割り当てるため、共有に置いても動く
pushd "%~dp0" || (
    echo [エラー] このフォルダーに移動できませんでした: %~dp0
    pause
    exit /b 1
)
title 業務ツール統合ランチャー - 診断起動 (この窓は閉じないでください)

python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [エラー] Python が見つかりません。
    echo.
    echo   https://www.python.org/downloads/ からインストールしてください。
    echo   インストールの最初の画面で
    echo   「Add python.exe to PATH」に必ずチェックを入れてください。
    echo.
    goto :failed
)

echo === 実行環境と設定の確認 ===
python launcher.py --check
if errorlevel 1 (
    echo.
    echo 上のメッセージを確認してください。
    goto :failed
)

echo.
echo === 起動 ===
echo この窓を閉じるとランチャーが終了します。
echo 業務ツールを止めるときは stop.bat を実行してください。
echo.
python launcher.py %*
if errorlevel 1 (
    echo.
    echo [エラー] 起動に失敗しました。上のメッセージを確認してください。
    echo          ログ: %LOCALAPPDATA%\BusinessToolsLauncher\logs の最新ファイル
    echo.
    goto :failed
)

echo.
echo 終了しました。この窓は閉じてしまいません。
pause
popd
endlocal
exit /b 0

:failed
pause
popd
endlocal
exit /b 1
