# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the macOS app bundle (also builds a Windows exe for testing).
#   pyinstaller --noconfirm --clean packaging/file-transferer.spec
import os
import sys

sys.path.insert(0, os.path.join(SPECPATH, "..", "src"))
from file_transferer import __version__  # noqa: E402

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[os.path.join(SPECPATH, "..", "src")],
    # tkinterdnd2: its native tkdnd library is collected by the hook in pyinstaller-hooks-contrib
    hiddenimports=["file_transferer.gui", "file_transferer.sender", "file_transferer.receiver", "tkinterdnd2"],
    excludes=["pytest", "unittest", "pydoc", "PyInstaller"],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="file-transferer",
    console=False,
    # Folder drops arrive via ::tk::mac::OpenDocument – no argv emulation needed
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
)

coll = COLLECT(exe, a.binaries, a.datas, name="file-transferer")

if sys.platform == "darwin":
  app = BUNDLE(
    coll,
    name="file-transferer.app",
    icon=os.path.join(SPECPATH, "file-transferer.icns") if os.path.exists(os.path.join(SPECPATH, "file-transferer.icns")) else None,
    bundle_identifier="app.file-transferer",
    version=__version__,
    info_plist={
        "CFBundleName": "File Transferer",
        "CFBundleDisplayName": "File Transferer",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        # Folders may be dropped onto the app / Dock icon.
        # LSHandlerRank "None": accepts drops but never becomes the default app for folders.
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "Folder",
            "CFBundleTypeRole": "Viewer",
            "LSHandlerRank": "None",
            "LSItemContentTypes": ["public.folder", "public.directory"],
        }],
        "NSDesktopFolderUsageDescription": "File Transferer reads folders to send and saves received folders.",
        "NSDocumentsFolderUsageDescription": "File Transferer reads folders to send and saves received folders.",
        "NSDownloadsFolderUsageDescription": "File Transferer saves received folders.",
        "NSRemovableVolumesUsageDescription": "File Transferer reads video folders from external drives.",
        "NSNetworkVolumesUsageDescription": "File Transferer reads video folders from network drives.",
        "NSFileProviderDomainUsageDescription": "File Transferer stores the packets in the cloud folder and reads them there.",
    },
)
