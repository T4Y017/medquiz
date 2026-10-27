# -*- mode: python ; coding: utf-8 -*-
# PyInstaller yapılandırması:  pyinstaller medquiz.spec
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

datas = [("assets", "assets")]
binaries = []
hiddenimports = []

# Anlamsal arama (fastembed + onnxruntime). Kurulu değilse atlanır; uygulama anahtar kelime aramasıyla çalışır.
try:
    import fastembed  # noqa: F401
    datas += collect_data_files("fastembed")
    hiddenimports += collect_submodules("fastembed")
    binaries += collect_dynamic_libs("onnxruntime")
    hiddenimports += ["onnxruntime", "tokenizers", "py_rust_stemmers"]
except ImportError:
    pass

# Hızlı PDF okuyucu (pdfium). Yoksa pypdf'e düşülür.
try:
    import pypdfium2  # noqa: F401
    datas += collect_data_files("pypdfium2") + collect_data_files("pypdfium2_raw")
    binaries += collect_dynamic_libs("pypdfium2_raw")
    hiddenimports += ["pypdfium2", "pypdfium2_raw"]
except ImportError:
    pass

hiddenimports += collect_submodules("anthropic") + collect_submodules("openai")
datas += collect_data_files("docx")

EXCLUDES = [
    "tkinter", "matplotlib", "pandas", "scipy", "IPython", "pytest", "torch", "tensorflow",
    "sounddevice", "cv2", "sklearn", "jupyter", "notebook",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtQuick3D",
    "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf",
    "PySide6.QtBluetooth", "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtSerialPort",
]

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MedQuiz",
    icon="assets/icon.ico",
    console=False,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name="MedQuiz", upx=False)
