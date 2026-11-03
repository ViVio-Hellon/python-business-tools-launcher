@echo off
rem  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
rem  Keep everything above the chcp line ASCII; see start.bat for why.
chcp 932 >nul 2>&1
rem ===================================================================
rem  日報複合ツール 起動確認の入口(業務ツール統合ランチャー 1.7 以降が使う)
rem
rem  終了コード: 0 = 使える / 2 = 準備中 / それ以外 = 動いていない
rem  デスクトップ版(日報複合ツール.exe)・ブラウザ版(Start.vbs)のどちらでも答えます。
rem  最後の1行は、ランチャーの起動中の窓に「準備の段階」として出ます。
rem  pause は置かない(ランチャーが繰り返し呼ぶ)。詳しくは docs\ランチャー連携.md
rem ===================================================================
setlocal
pushd "%~dp0" || exit /b 1
python process_manager.py --check
set "code=%errorlevel%"
popd
endlocal & exit /b %code%
