import os

import flet as ft

from src.main_mobile import main

if __name__ == "__main__":
    ft.app(target=main, assets_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets"))