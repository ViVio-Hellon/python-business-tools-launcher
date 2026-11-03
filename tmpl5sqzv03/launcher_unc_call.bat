@echo off
rem Business Tools Launcher: runs a .bat that is on a shared folder (\\server\...).
rem cmd cannot use \\server\... as the current folder, so map it with pushd
rem and call the .bat through the mapped drive letter. The drive is kept so that
rem programs started in the background keep working; the launcher reuses it.
pushd "%~dp1" || exit /b 1
call "%CD%\%~nx1" %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%
