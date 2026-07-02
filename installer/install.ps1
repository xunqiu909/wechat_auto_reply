$ErrorActionPreference = 'Stop'

$appName = 'WxAutoAutoReply'
$installDir = Join-Path $env:LOCALAPPDATA $appName
$desktopDir = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopDir 'WxAuto Auto Reply.lnk'

New-Item -ItemType Directory -Force -Path $installDir | Out-Null

Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'wxauto_auto_reply.exe') -Destination $installDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'user_guide.md') -Destination $installDir -Force

$uninstallPath = Join-Path $installDir 'uninstall.cmd'
$uninstallText = @"
@echo off
del "%USERPROFILE%\Desktop\WxAuto Auto Reply.lnk" 2>nul
rmdir /s /q "%LOCALAPPDATA%\WxAutoAutoReply"
echo Uninstalled WxAuto Auto Reply.
pause
"@
Set-Content -LiteralPath $uninstallPath -Value $uninstallText -Encoding ASCII

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $installDir 'wxauto_auto_reply.exe'
$shortcut.WorkingDirectory = $installDir
$shortcut.IconLocation = Join-Path $installDir 'wxauto_auto_reply.exe'
$shortcut.Save()

Write-Host "Installed to: $installDir"
Write-Host "Desktop shortcut: $shortcutPath"
Start-Process explorer.exe $installDir
