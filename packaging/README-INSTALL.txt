File Transferer – installation on a Mac (once per Mac)
==================================================

1. Unzip the download. The folder contains:
     file-transferer.app
     Install File Transferer.command
     README-INSTALL.txt

2. Double-click "Install File Transferer.command".
   Because the app is not notarized by Apple, macOS blocks the script the
   first time. Then:
     - right-click the script → Open → Open, or
     - macOS 15 and later: System Settings › Privacy & Security, scroll down,
       "Open Anyway" next to the message about the script.
   Alternative in Terminal (no warning):
     bash "/path/to/Install File Transferer.command"

3. The script copies the app to Applications, removes the macOS block on the app
   and asks four questions:
     - cloud folder for the deliveries (default: Google Drive) – the same
       folder on every Mac (see "Shared folder" below)
     - name of this Mac (shown to the receiver)
     - whether this Mac mainly sends or receives
     - whether File Transferer should start at login (recommended on the receiving Mac)

4. On the first access, macOS asks whether File Transferer may access Google Drive,
   Movies or Downloads. Please allow it.

Shared folder
-------------
All Macs need to see the same folder in Google Drive:
  - simplest: all Macs are signed in to the same Google account, or
  - one account creates a folder (e.g. "File Transferer"), shares it with the other
    accounts as editor, and the other accounts add it to "My Drive" (in the
    browser: right-click → Organize → Add shortcut), or
  - a shared drive is used.

Updating
--------
Run the new version's script again; settings are kept.

Uninstalling
------------
Delete /Applications/file-transferer.app. Settings and the delivery list are in
~/Library/Application Support/File Transferer, the log in ~/Library/Logs/File Transferer,
and the start-at-login entry is
~/Library/LaunchAgents/app.file-transferer.login.plist.
