import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import flet as ft

from src.ui.main import NurBooksApp


def main(page: ft.Page):
    page.title = "NurBooks Mobile"
    NurBooksApp(page)


if __name__ == "__main__":
    ft.app(target=main, assets_dir=os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets"))
