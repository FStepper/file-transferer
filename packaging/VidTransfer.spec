# -*- mode: python ; coding: utf-8 -*-
# PyInstaller-Spezifikation für das macOS-App-Bundle.
#   pyinstaller --noconfirm --clean packaging/VidTransfer.spec
import os
import sys

sys.path.insert(0, os.path.join(SPECPATH, "..", "src"))
from vidtransfer import __version__  # noqa: E402

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[os.path.join(SPECPATH, "..", "src")],
    hiddenimports=["vidtransfer.gui", "vidtransfer.sender", "vidtransfer.receiver"],
    excludes=["pytest", "unittest", "pydoc", "PyInstaller"],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VidTransfer",
    console=False,
    # Ordner-Drops kommen über ::tk::mac::OpenDocument an – keine argv-Emulation
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
)

coll = COLLECT(exe, a.binaries, a.datas, name="VidTransfer")

if sys.platform == "darwin":
  app = BUNDLE(
    coll,
    name="VidTransfer.app",
    icon=os.path.join(SPECPATH, "VidTransfer.icns") if os.path.exists(os.path.join(SPECPATH, "VidTransfer.icns")) else None,
    bundle_identifier="app.vidtransfer.VidTransfer",
    version=__version__,
    info_plist={
        "CFBundleName": "VidTransfer",
        "CFBundleDisplayName": "VidTransfer",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        # Ordner dürfen auf das App-/Dock-Symbol gezogen werden.
        # LSHandlerRank "None": nimmt Drops an, wird aber nie Standard-App für Ordner.
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "Ordner",
            "CFBundleTypeRole": "Viewer",
            "LSHandlerRank": "None",
            "LSItemContentTypes": ["public.folder", "public.directory"],
        }],
        "NSDesktopFolderUsageDescription": "VidTransfer liest Ordner, die gesendet werden sollen, und speichert empfangene Ordner.",
        "NSDocumentsFolderUsageDescription": "VidTransfer liest Ordner, die gesendet werden sollen, und speichert empfangene Ordner.",
        "NSDownloadsFolderUsageDescription": "VidTransfer speichert empfangene Ordner.",
        "NSRemovableVolumesUsageDescription": "VidTransfer liest Video-Ordner von externen Laufwerken.",
        "NSNetworkVolumesUsageDescription": "VidTransfer liest Video-Ordner von Netzlaufwerken.",
        "NSFileProviderDomainUsageDescription": "VidTransfer legt die Pakete in Google Drive ab und liest sie dort.",
    },
)
