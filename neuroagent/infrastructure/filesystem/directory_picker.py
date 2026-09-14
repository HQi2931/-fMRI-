"""Local operating-system directory picker for the desktop-first API."""

from __future__ import annotations


class SystemDirectoryPicker:
    """Open the native directory chooser on the machine running the API."""

    def pick_directory(self) -> str | None:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                selected = filedialog.askdirectory(title="选择 rs-fMRI 工作区")
            finally:
                root.destroy()
            return selected or None
        except Exception:
            return None
