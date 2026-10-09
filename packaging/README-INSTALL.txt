VidTransfer – installation on a Mac (once per Mac)
==================================================

1. Unzip the download. The folder contains:
     VidTransfer.app
     Install VidTransfer.command
     README-INSTALL.txt

2. Double-click "Install VidTransfer.command".
   Because the app is not notarized by Apple, macOS blocks the script the
   first time. Then:
     - right-click the script → Open → Open, or
     - macOS 15 and later: System Settings › Privacy & Security, scroll down,
       "Open Anyway" next to the message about the script.
   Alternative in Terminal (no warning):
     bash "/path/to/Install VidTransfer.command"

3. The script copies the app to Applications, removes the macOS block on the app
   and asks four questions:
     - Google Drive folder for the deliveries – the same folder on every Mac
       (see "Shared folder" below)
     - name of this Mac (shown to the receiver)
     - whether this Mac mainly sends or receives
     - whether VidTransfer should start at login (recommended on the receiving Mac)

4. On the first access, macOS asks whether VidTransfer may access Google Drive,
   Movies or Downloads. Please allow it.

Shared folder
-------------
All Macs need to see the same folder in Google Drive:
  - simplest: all Macs are signed in to the same Google account, or
  - one account creates a folder (e.g. "VidTransfer"), shares it with the other
    accounts as editor, and the other accounts add it to "My Drive" (in the
    browser: right-click → Organize → Add shortcut), or
  - a shared drive is used.

Updating
--------
Run the new version's script again; settings are kept.

Uninstalling
------------
Delete /Applications/VidTransfer.app. Settings and the delivery list are in
~/Library/Application Support/VidTransfer, the log in ~/Library/Logs/VidTransfer,
and the start-at-login entry is
~/Library/LaunchAgents/app.vidtransfer.VidTransfer.login.plist.
