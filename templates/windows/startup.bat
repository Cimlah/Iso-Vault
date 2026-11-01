@echo off
title Iso-Vault: starting Windows Setup
echo Starting the network...
wpeinit

set attempts=0
:connect
set /a attempts+=1
net use Z: \\@@SERVER_IP@@\isovault\@@ISO_ID@@ @@SMB_PASSWORD@@ /user:@@SERVER_IP@@\isovault >nul 2>&1
if not errorlevel 1 goto setup
if %attempts% geq 30 goto failed
echo Waiting for the installer share at @@SERVER_IP@@ (attempt %attempts% of 30)...
ping -n 3 127.0.0.1 >nul
goto connect

:setup
echo Starting Windows Setup...
Z:\setup.exe
goto end

:failed
echo.
echo Could not connect to \\@@SERVER_IP@@\isovault\@@ISO_ID@@
echo Windows PE may not include a driver for this network adapter, or port 445
echo may be blocked on the server. Dropping to a command prompt.
echo.

:end
cmd
