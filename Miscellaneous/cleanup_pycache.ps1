$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# Remove all Python bytecode cache directories.
Get-ChildItem -Path $root -Recurse -Directory -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq "__pycache__" } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# Remove standalone Python bytecode files.
Get-ChildItem -Path $root -Recurse -File -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
    Remove-Item -Force -ErrorAction SilentlyContinue

Write-Host "Cleanup done: removed __pycache__ folders and .pyc/.pyo files."
