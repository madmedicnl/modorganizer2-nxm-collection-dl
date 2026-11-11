import json
import re
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from .debug import qDebug
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
)

from . import __meta__, var
from .api import choiceText, fetchManifest, choicesFromManifest


def game_version(exe_path) -> str:
    """The version stamped on the game's own exe (ProductVersion), or "".

    A collection is built for one game version, and mods that hook the game
    break quietly on another. Read from the exe rather than from a file the
    collection ships, because it is the only thing on this machine that says
    what is actually installed.
    """
    try:
        import ctypes
        from ctypes import wintypes

        path = str(exe_path)
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        value = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(
            buf,
            "\\VarFileInfo\\Translation",
            ctypes.byref(value),
            ctypes.byref(length),
        ):
            return ""
        words = ctypes.cast(value, ctypes.POINTER(wintypes.WORD))
        lang, codepage = words[0], words[1]
        sub = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\ProductVersion"
        if not ctypes.windll.version.VerQueryValueW(
            buf, sub, ctypes.byref(value), ctypes.byref(length)
        ):
            return ""
        return ctypes.wstring_at(value, length.value).strip("\x00")
    except Exception as e:
        trace(f"could not read the game version: {e}")
        return ""


def trace(message: str) -> None:
    """One breadcrumb a human can read after the fact.

    Two places, on purpose: `qDebug` reaches MO2's log only when its log level
    is Debug, and the log inside the install dialog cannot repaint once the
    event loop blocks — so a stuck batch install otherwise leaves no evidence
    at all. This file is written from Python and does not care about either.
    """
    qDebug(f"[NXMColDL step] {message}")
    try:
        with open(Path(__file__).with_name("steps.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + message + "\n")
    except Exception:
        pass


class stepSelectCollection(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NXM Collection Installer - Select Collection")
        self.setMinimumWidth(400)

        self.collections_list = []
        self.current_metadata = None
        self.network_manager = None

        layout = QVBoxLayout()

        # Dropdown at the top
        self.label = QLabel("Select a downloaded collection to install:")
        layout.addWidget(self.label)

        self.dropdown = QComboBox()
        self.dropdown.currentIndexChanged.connect(self.on_selection_changed)
        layout.addWidget(self.dropdown)

        layout.addSpacing(10)

        # Info display section
        infoBox = QHBoxLayout()

        self.thumb_label = QLabel()
        self.thumb_label.setMaximumHeight(128)
        self.thumb_label.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        infoBox.addWidget(self.thumb_label)

        self.info = QLabel("")
        self.info.setWordWrap(True)
        infoBox.addWidget(self.info)

        layout.addLayout(infoBox)

        layout.addSpacing(10)

        # Submit button
        self.submit_btn = QPushButton("Next")
        self.submit_btn.clicked.connect(self.submit)
        self.submit_btn.setEnabled(False)  # Disabled until a collection is loaded
        layout.addWidget(self.submit_btn)

        self.setLayout(layout)

        # What this machine is running; filled in on first ask (see
        # installed_game_version).
        self._game_version = None

    def showEvent(self, event):
        """Refresh collections list every time the dialog is shown"""
        super().showEvent(event)
        self.refreshCollections()

    def refreshCollections(self):
        """Reload and refresh the collections list"""
        # Clear current dropdown
        self.dropdown.blockSignals(True)
        self.dropdown.clear()
        self.collections_list = []
        self.current_metadata = None
        self.dropdown.blockSignals(False)

        # Load collections
        plugin_instance = getattr(__meta__, "_install_plugin", None)
        if not plugin_instance or not hasattr(plugin_instance, "_organizer"):
            QMessageBox.critical(self, "Error", "Failed to access Mod Organizer")
            qDebug("[NXMColDL] Failed to get plugin instance")
            self.close()
            return

        base_path = Path(plugin_instance._organizer.basePath())
        self.collections_list = var.listCollectionMetadata(base_path)
        qDebug(
            f"[NXMColDL] Found {len(self.collections_list)} collection(s) to install"
        )

        if not self.collections_list:
            QMessageBox.information(
                self,
                "No Collections Found",
                "No downloaded collections found.\n\nPlease download a collection first using the Download Collection tool.",
            )
            qDebug("[NXMColDL] No collections found")
            self.close()
            return

        # Populate dropdown
        for game, collection_id, revision, metadata_file in self.collections_list:
            # Load metadata to get the display name
            try:
                with open(metadata_file, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                    name = metadata.get("name", collection_id)
                    # author = metadata.get("author", "Unknown")
                    display_text = f"{name} (Rev {revision}) - {game}"
                    self.dropdown.addItem(display_text)
            except Exception as e:
                qDebug(f"[NXMColDL] Error loading metadata from {metadata_file}: {e}")
                self.dropdown.addItem(f"{collection_id} (Rev {revision}) - {game}")

        # Trigger initial selection
        if self.dropdown.count() > 0:
            self.on_selection_changed(0)

    def on_selection_changed(self, index):
        """Handle dropdown selection change"""
        if index < 0 or index >= len(self.collections_list):
            return

        game, collection_id, revision, metadata_file = self.collections_list[index]

        # Load metadata
        try:
            with open(metadata_file, "r", encoding="utf-8") as f:
                self.current_metadata = json.load(f)

            # Update display
            name = self.current_metadata.get("name", "Unknown Collection")
            author = self.current_metadata.get("author", "Unknown Author")
            summary = self.current_metadata.get("summary", "No description available.")
            thumbnail = self.current_metadata.get("thumbnail")
            total_mods = self.current_metadata.get("totalMods", 0)
            timestamp = self.current_metadata.get("timestamp", "Unknown")

            # Clean up summary for display
            summary = (
                var.cleanJson(summary, True) if summary else "No description available."
            )

            # Update info label
            versions = self.current_metadata.get("gameVersions") or []
            declared = ", ".join(versions) if versions else "not stated"
            size_gib = (self.current_metadata.get("declaredTotalSize") or 0) / 1024**3
            notes = (self.current_metadata.get("installationInfo") or "").strip()
            # What this machine actually has, so a collection built for another
            # game version is visible before 30 GB of mods go on, not after.
            installed = self.installed_game_version()
            verdict = ""
            if versions and installed:
                if any(
                    installed == v or installed.startswith(v) or v.startswith(installed)
                    for v in versions
                ):
                    verdict = f'<span style="color:green">matches {installed}</span>'
                else:
                    verdict = (
                        f'<span style="color:red">this install is {installed} —'
                        " mods may break</span>"
                    )
            self.info.setText(f"""
				<h2 style="margin:0;padding:0">{name}</h2>
				<br>
				by <i>{author}</i>
				<br>
				<br>
				{summary}
				<br>
				<br>
				<b>Total Mods:</b> {total_mods}
				<br>
				<b>Downloaded:</b> {timestamp.split("T")[0] if "T" in timestamp else timestamp}
				<br>
				<b>Built for game version:</b> {declared} {verdict}
				<br>
				<b>Size when installed:</b> {size_gib:.1f} GiB
				{'<br><br><b>Author&rsquo;s install notes:</b><br>' + notes if notes else ''}
			""")

            # Update thumbnail
            if thumbnail:
                self.network_manager = var.loadThumbnail(
                    thumbnail, self.thumb_label, self.network_manager
                )
            else:
                self.thumb_label.clear()

            self.submit_btn.setEnabled(True)

            qDebug(f"[NXMColDL] Selected collection: {name} (Rev {revision})")

        except Exception as e:
            QMessageBox.critical(
                self, "Error", f"Failed to load collection metadata: {e}"
            )
            qDebug(f"[NXMColDL] Error loading metadata: {e}")
            self.submit_btn.setEnabled(False)

    def installed_game_version(self) -> str:
        """The game's own version here, or "" when it cannot be read."""
        if self._game_version is None:
            self._game_version = ""
            try:
                plugin = getattr(__meta__, "_install_plugin", None)
                organizer = plugin._organizer
                game = organizer.managedGame()
                exe = Path(game.gameDirectory().absolutePath()) / game.binaryName()
                self._game_version = game_version(exe)
            except Exception as e:
                trace(f"could not locate the game to read its version: {e}")
        return self._game_version

    def submit(self):
        """Proceed to next step with selected collection"""
        if not self.current_metadata:
            QMessageBox.critical(self, "Error", "No collection selected")
            return

        # Store metadata in var for use in subsequent steps
        var.uri = self.current_metadata.get("uri")
        var.collectionId = self.current_metadata.get("collectionId") or 0
        var.revisionId = self.current_metadata.get("revisionId") or 0
        var.choices = self.current_metadata.get("choices") or {}
        var.game = self.current_metadata.get("game")
        var.collection = self.current_metadata.get("collection")
        var.revision = self.current_metadata.get("revision")
        var.author = self.current_metadata.get("author", "Unknown Author")
        var.name = self.current_metadata.get("name", "Unknown Collection")
        var.summary = self.current_metadata.get("summary", "No description available.")
        var.thumbnail = self.current_metadata.get("thumbnail")
        var.essentialMods = self.current_metadata.get("essentialMods", [])
        var.chosenOptional = self.current_metadata.get("chosenOptional", [])
        var.externalMods = self.current_metadata.get("externalMods", [])

        qDebug(f"[NXMColDL] Loaded collection: {var.name}")
        qDebug(f"[NXMColDL] Essential mods: {len(var.essentialMods)}")
        qDebug(f"[NXMColDL] Optional mods: {len(var.chosenOptional)}")

        self.close()

        # Proceed to installation step. The activation happens after the
        # dialog has closed: inside it, MO2's refresh deadlocks (see the note
        # in startInstallation).
        installer = stepInstallMods(self.parent())
        installer.exec()
        installer.applyInstalled()


class stepInstallMods(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NXM Collection Installer - Installing Mods")
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)

        layout = QVBoxLayout()

        # Title
        title = QLabel(f"Installing: {var.name}")
        title.setStyleSheet("font-weight: bold; font-size: 14pt;")
        layout.addWidget(title)

        # Progress info
        self.progress_label = QLabel("Preparing installation...")
        layout.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        layout.addWidget(self.progress_bar)

        # Log area
        log_label = QLabel("Installation Log:")
        layout.addWidget(log_label)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(200)
        layout.addWidget(self.log_text)

        # Close button (initially disabled)
        self.close_btn = QPushButton("Close")
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        layout.addWidget(self.close_btn)

        self.setLayout(layout)

        # What was installed and at which priority. Applied after this dialog
        # has closed; see the note where it is filled in.
        self.pending_activation = []
        self.organizer = None

        # Start installation after dialog is shown
        QTimer.singleShot(100, self.startInstallation)

    def applyInstalled(self):
        """Switch on and order what was installed, once MO2 owns the loop again.

        Called after `exec()` has returned, so this runs from MO2's own event
        loop rather than from a nested one — that nested loop is what made
        `setActive` hang.
        """
        if self.organizer is None or not self.pending_activation:
            return
        trace(f"activation start: {len(self.pending_activation)} mods")
        modlist = self.organizer.modList()
        for internal_name, priority in self.pending_activation:
            try:
                modlist.setActive(internal_name, True)
                if not modlist.setPriority(internal_name, priority):
                    qDebug(
                        f"[NXMColDL] could not give {internal_name!r} priority"
                        f" {priority}"
                    )
            except Exception as e:
                qDebug(f"[NXMColDL] activating {internal_name!r} failed: {e}")
        trace("activation done")

    def log(self, message, level="info"):
        color = "black"
        if level == "error":
            color = "red"
        elif level == "warning":
            color = "orange"
        elif level == "success":
            color = "green"

        self.log_text.append(f'<span style="color: {color};">{message}</span>')
        qDebug(f"[NXMColDL Install] {message}")
        QApplication.processEvents()

    def startInstallation(self):
        """Main installation process"""
        qDebug(f"[NXMColDL] Starting installation: {var.name}")
        try:
            plugin_instance = getattr(__meta__, "_install_plugin", None)
            if not plugin_instance or not hasattr(plugin_instance, "_organizer"):
                self.log("Failed to access Mod Organizer", "error")
                self.close_btn.setEnabled(True)
                return

            organizer = plugin_instance._organizer
            self.organizer = organizer
            modlist = organizer.modList()

            # Get all mods to install in order
            mods_to_install = var.essentialMods + var.chosenOptional
            self.progress_bar.setMaximum(len(mods_to_install))

            self.log(f"Total mods to install: {len(mods_to_install)}")

            # **The collection's FOMOD answers, if they are not cached yet.** A
            # collection downloaded before this existed has none; fetching them
            # here means an operator does not have to download it again.
            if not var.choices and var.collectionId and var.revisionId:
                self.log("Looking up the collection's own choices...")
                manifest = fetchManifest(var.collectionId, var.revisionId)
                var.choices = choicesFromManifest(manifest)
                if var.choices:
                    self.log(
                        f"The collection's choices are known for {len(var.choices)}"
                        " mod(s).",
                        "success",
                    )
                else:
                    self.log(
                        "The collection's own choices are not available (MO2's"
                        " Nexus key, or Nexus not answering); each FOMOD will ask"
                        " you.",
                        "warning",
                    )
            versions = var.gameVersions or []
            if versions:
                trace(f"collection built for game version(s) {versions}")
                self.log(
                    "Collection is built for game version: " + ", ".join(versions)
                )
            if var.declaredTotalSize:
                self.log(
                    f"The collection is {var.declaredTotalSize / 1024**3:.1f} GiB"
                    " when installed."
                )
            # **The FOMOD answers are not obtainable.** Nexus' public GraphQL
            # exposes no choices for a collection mod, and the manifest that
            # carries them needs an API key, so a mod with a FOMOD still asks
            # its own questions. Said once, here, rather than leaving the
            # operator to wonder why the collection did not decide for them.
            self.log(
                "Mods with a FOMOD installer will ask you their own questions:"
                " Nexus does not publish the collection's answers.",
                "warning",
            )

            # Get initial mod count to determine starting priority
            # Collection mods will be placed at the end of the current mod list
            initial_mod_count = len(modlist.allMods())
            base_priority = initial_mod_count
            self.log(f"Existing mods in list: {initial_mod_count}")
            self.log(f"Collection mods will start at priority: {base_priority}")
            self.log("")

            # Find downloaded files
            downloads_path = Path(organizer.downloadsPath())
            self.log(f"Searching for downloads in: {downloads_path}")

            # Build a mapping of (modId, fileId) -> download_path
            download_map = self.buildDownloadMap(downloads_path)
            self.log(f"Found {len(download_map)} downloaded files")
            self.log("")

            installed_mod_names = set()
            for mod in modlist.allMods():
                try:
                    installed_mod_names.add(mod.name().casefold())
                except Exception:
                    continue

            # Install each mod in order
            installed_mods = []
            skipped_mods = []
            failed_mods = []
            for idx, mod_info in enumerate(mods_to_install, 1):
                self.progress_label.setText(
                    f"Installing mod {idx}/{len(mods_to_install)}"
                )
                self.progress_bar.setValue(idx - 1)

                mod_id = mod_info["file"]["mod"]["modId"]
                file_id = mod_info["file"]["fileId"]
                mod_name = mod_info["file"]["mod"]["name"]
                file_name = mod_info["file"]["name"]

                self.log(f"[{idx}/{len(mods_to_install)}] Processing: {mod_name}")
                self.log(f"  File: {file_name} (ModID: {mod_id}, FileID: {file_id})")

                # **What the collection itself chose**, where Nexus publishes it.
                # MO2's installer cannot be handed answers from a plugin, so the
                # wizard still asks — but it asks an operator who can see what
                # the maker picked. Said before the "already installed" skip, so
                # a collection that is on disk already can be read for its
                # answers too.
                choice = choiceText(var.choices.get(f"{mod_id}:{file_id}"))
                if choice:
                    self.log(f"  The collection chose: {choice}", "warning")

                if mod_name.casefold() in installed_mod_names:
                    self.log("  Already installed - skipping", "warning")
                    skipped_mods.append(mod_name)
                    self.log("")
                    continue

                # Find downloaded file
                download_key = (int(mod_id), int(file_id))
                if download_key not in download_map:
                    self.log("  ERROR: Not found in downloads - skipping", "error")
                    failed_mods.append(mod_name)
                    self.log("")
                    continue

                download_path = download_map[download_key]
                self.log(f"  Found: {download_path.name}")


                # **A mod name is a Windows folder name.** A title like
                # "X4: Foundations" makes MO2 ask for a valid name in a prompt
                # of its own, and that prompt never reaches the loop this
                # dialog runs in: the install sits there for ever. Sanitised
                # here, the mod is named without asking. See upstream #34.
                safe_name = re.sub(r'[\\/*?:"<>|]', "", mod_name).strip()
                if not safe_name:
                    safe_name = f"mod {mod_id}"
                if safe_name != mod_name:
                    trace(f"name sanitised: {mod_name!r} -> {safe_name!r}")

                # **Breadcrumbs in MO2's log, one per step.** The log in this
                # window stops repainting the moment the event loop blocks, so
                # a hang here says nothing about where it hung; the log file
                # does, and it is the only evidence a stuck install leaves.
                trace(f"[{idx}/{len(mods_to_install)}] installMod start: {safe_name}")
                # MO2's installer — and, for a FOMOD, its wizard — is a window
                # of its own and blocks until it is answered. Hidden first, so
                # that wizard is not buried under this dialog.
                try:
                    installed_mod = organizer.installMod(str(download_path), safe_name)
                finally:
                    trace(f"[{idx}/{len(mods_to_install)}] installMod returned")
                    self.show()
                    self.raise_()
                    self.activateWindow()
                    QApplication.processEvents()
                try:
                    if installed_mod:
                        internal_name = installed_mod.name()
                        self.log(f"  Installed as: {internal_name}", "success")

                        # **Activated later, not here.** Enabling a mod makes
                        # MO2 refresh its mod list and deployment, and doing
                        # that from inside this dialog's own event loop
                        # deadlocks MO2: the refresh starts ("refresher: using
                        # 10 threads") and never returns, because the thread it
                        # wants is the one running this dialog. The list is
                        # collected and applied by `applyInstalled()` once the
                        # dialog is gone — which is the same place a click on a
                        # checkbox would run from.
                        target_priority = base_priority + len(installed_mods)
                        self.pending_activation.append((internal_name, target_priority))
                        installed_mods.append(internal_name)
                        installed_mod_names.add(internal_name.casefold())
                    else:
                        self.log(
                            "  ERROR: Installation failed or was cancelled", "error"
                        )
                        failed_mods.append(mod_name)
                except Exception as e:
                    self.log(f"  ERROR: Installation error: {e}", "error")
                    failed_mods.append(mod_name)

                self.log("")

            # Final summary
            self.progress_bar.setValue(len(mods_to_install))
            self.progress_label.setText("Installation complete!")
            self.log("=" * 50)
            self.log("Installation Summary:", "success")
            self.log(f"  Total mods: {len(mods_to_install)}")
            self.log(f"  Successfully installed: {len(installed_mods)}")
            self.log(f"  Skipped already installed: {len(skipped_mods)}")
            self.log(f"  Failed: {len(failed_mods)}")
            qDebug(
                f"[NXMColDL] Installation complete: {len(installed_mods)}/{len(mods_to_install)} succeeded"
            )

            if self.pending_activation:
                self.log(
                    f"  {len(self.pending_activation)} of them will be switched on"
                    " when this window closes.",
                    "warning",
                )

            if skipped_mods:
                self.log("", "warning")
                self.log(
                    "Some mods were already installed and were skipped.",
                    "warning",
                )

            if failed_mods:
                self.log("", "warning")
                self.log(
                    "Some mods were not installed. Make sure all mods are downloaded first.",
                    "warning",
                )

        except Exception as e:
            self.log(f"Fatal error during installation: {e}", "error")
        finally:
            self.close_btn.setEnabled(True)

    def buildDownloadMap(self, downloads_path: Path):
        download_map = {}

        if not downloads_path.exists():
            self.log(f"Downloads directory does not exist: {downloads_path}", "error")
            qDebug(f"[NXMColDL] Downloads path not found: {downloads_path}")
            return download_map

        qDebug(f"[NXMColDL] Building download map from: {downloads_path}")
        # Iterate through all .meta files in downloads directory
        for meta_file in downloads_path.glob("*.meta"):
            try:
                # The actual download file has the same name without .meta extension
                download_file = meta_file.with_suffix("")

                # Only consider files that exist and are not directories
                if not download_file.exists() or download_file.is_dir():
                    continue

                # Parse the .meta file (it's an INI-style file)
                mod_id = None
                file_id = None

                with open(meta_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("modID="):
                            mod_id = int(line.split("=", 1)[1])
                        elif line.startswith("fileID="):
                            file_id = int(line.split("=", 1)[1])

                        # Early exit if we found both
                        if mod_id is not None and file_id is not None:
                            break

                if mod_id is not None and file_id is not None:
                    download_map[(mod_id, file_id)] = download_file
                else:
                    qDebug(
                        f"[NXMColDL] Incomplete metadata in {meta_file.name}: modID={mod_id}, fileID={file_id}"
                    )

            except (ValueError, IOError) as e:
                qDebug(f"[NXMColDL] Error parsing {meta_file.name}: {e}")
            except Exception as e:
                qDebug(f"[NXMColDL] Unexpected error parsing {meta_file.name}: {e}")

        return download_map
