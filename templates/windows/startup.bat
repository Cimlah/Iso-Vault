@echo off
title Iso-Vault: starting Windows Setup
echo Starting the network...
wpeinit
@@DRIVER_COMMANDS@@

rem A MAC address looks the same in every language, unlike the text around it.
set mac=[0-9A-F][0-9A-F]-[0-9A-F][0-9A-F]-[0-9A-F][0-9A-F]-

set attempts=0
:connect
set /a attempts+=1
net use Z: \\@@SERVER_IP@@\isovault\@@ISO_ID@@ @@SMB_PASSWORD@@ /user:@@SERVER_IP@@\isovault >X:\isovault-net.txt 2>&1
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
echo.
echo The error was:
type X:\isovault-net.txt
echo.
echo This computer's network settings:
ipconfig /all
echo.
rem This only chooses the advice below, so that a wrong answer can't stop a boot that would work.
ipconfig /all | findstr /r /i "%mac%" >nul || goto no_adapter
echo Check that this computer has an IP address above, and that port 445 is open on the server.
goto prompt

:no_adapter
echo No network adapter is listed above, so Windows PE probably has no driver for this
echo computer's network card. To fix this, copy the card's Windows driver into the
echo drivers\@@DRIVER_FOLDER@@ folder on the Iso-Vault server, then boot this computer again.
echo See docs/troubleshooting.md for how.

:prompt
echo.
echo Dropping to a command prompt.
echo.

:end
cmd
