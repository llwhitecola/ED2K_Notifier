# Rebuild standalone ED2K_Notifier.exe
Set-Location $PSScriptRoot

& ".\.venv\Scripts\pyinstaller.exe" `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "ED2K_Notifier" `
    --hidden-import "Crypto.Hash.MD4" `
    --hidden-import "socks" `
    --collect-binaries "Crypto" `
    --exclude-module "pytest" `
    --exclude-module "PySide6.QtWebEngineCore" `
    --exclude-module "PySide6.QtWebEngineWidgets" `
    --exclude-module "PySide6.QtWebEngineQuick" `
    --exclude-module "PySide6.QtPdf" `
    --exclude-module "PySide6.QtPdfWidgets" `
    --exclude-module "PySide6.QtQuick" `
    --exclude-module "PySide6.QtQml" `
    --exclude-module "PySide6.QtMultimedia" `
    --exclude-module "PySide6.QtMultimediaWidgets" `
    --exclude-module "PySide6.Qt3DCore" `
    --exclude-module "PySide6.Qt3DRender" `
    --exclude-module "PySide6.QtCharts" `
    --exclude-module "PySide6.QtDataVisualization" `
    --exclude-module "PySide6.QtOpenGL" `
    --exclude-module "PySide6.QtOpenGLWidgets" `
    --exclude-module "PySide6.QtSql" `
    --exclude-module "PySide6.QtTest" `
    --exclude-module "PySide6.QtNetwork" `
    main.py
