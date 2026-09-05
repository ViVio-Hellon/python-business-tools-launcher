@echo off
rem  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
rem  Keep everything above the chcp line ASCII; see start_debug.bat for why.
chcp 932 >nul 2>&1
rem ===================================================================
rem  ランチャーが起動した業務ツールを停止する (要件定義書 §10)
rem
rem  ランチャーから起動したツールだけを止めます。同じPCで動いている
rem  別の Python アプリは影響を受けません(基盤仕様書 2.8)。
rem
rem      stop.bat            正常終了を要求して止める
rem      stop.bat --status   いま何が動いているか見るだけ
rem      stop.bat --force    実行中の処理を中断してでも止める
rem ===================================================================
setlocal

rem  共有フォルダーに置かれていても動くよう pushd を使う
pushd "%~dp0" || (
    echo [エラー] このフォルダーに移動できませんでした: %~dp0
    pause
    exit /b 1
)
title 業務ツール統合ランチャー - 停止

python --version >nul 2>&1
if errorlevel 1 (
    echo [エラー] Python が見つかりません。
    goto :failed
)

python process_manager.py %*
if errorlevel 1 (
    echo.
    echo 止められなかったものがあります。上のメッセージを確認してください。
    echo 実行中の処理を中断してよければ、次を実行してください:
    echo     stop.bat --force
    echo.
    goto :failed
)
popd
endlocal
exit /b 0

:failed
pause
popd
endlocal
exit /b 1
