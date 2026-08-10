# Refresh package-manager PATH additions in the current PowerShell process.
# This is needed immediately after a non-interactive winget installation.
$machineToolPath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$userToolPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$env:Path = "$machineToolPath;$userToolPath;$env:Path"
