import os
import shutil
import tempfile
import threading
import time

import flet as ft

from src.core.downloader import Downloader
from src.core.models import Book

try:
    import pypdfium2 as pdfium
    PDFIUM_AVAILABLE = True
except ImportError:
    PDFIUM_AVAILABLE = False

# Глобальная папка для временных файлов
TEMP_DIR = os.path.join(tempfile.gettempdir(), "nurbooks_pdf_reader")


def cleanup_temp_files():
    """Очищает все временные файлы"""
    try:
        if os.path.exists(TEMP_DIR):
            shutil.rmtree(TEMP_DIR, ignore_errors=True)
    except Exception as e:
        print(f"Ошибка очистки временных файлов: {e}")


def _convert_github_url(url: str) -> str:
    """Конвертирует GitHub ссылку в raw-ссылку."""
    if not url:
        return url
    if "raw.githubusercontent.com" in url:
        return url
    if "github.com" in url and "/blob/" in url:
        return url.replace("/blob/", "/raw/")
    return url


def _save_bitmap_png(bitmap, output_path: str):
    """Сохраняет PdfBitmap в PNG без Pillow/numpy (их нет на Android)."""
    import struct
    import zlib

    width = bitmap.width
    height = bitmap.height
    stride = bitmap.stride
    n_channels = bitmap.n_channels
    buf = bitmap.buffer

    # Собираем scanlines (RGB), каждый ряд с байтом фильтра 0
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        row = y * stride
        for x in range(width):
            i = row + x * n_channels
            raw.append(buf[i])      # R
            raw.append(buf[i + 1])  # G
            raw.append(buf[i + 2])  # B

    compressed = zlib.compress(bytes(raw), 9)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        chunk_data = struct.pack(">I", len(data)) + tag + data
        return chunk_data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", ihdr)
    png += _chunk(b"IDAT", compressed)
    png += _chunk(b"IEND", b"")

    with open(output_path, "wb") as f:
        f.write(png)


class PDFReaderPage:
    """
    PDF-ридер с надёжным обновлением через файлы.
    """

    def __init__(self, page: ft.Page, book: Book, on_back=None, downloader: Downloader | None = None, bookmarks=None, go_to_page: int | None = None):
        self.page = page
        self.book = book
        self.on_back = on_back
        self.downloader = downloader or Downloader()
        self.bookmarks = bookmarks or []
        self.go_to_page = go_to_page
        # Регистрируем Ctrl+F
        self._prev_keyboard = getattr(page, 'on_keyboard_event', None)
        page.on_keyboard_event = self._handle_keyboard

        # Состояние
        self.pdf_doc = None
        self.current_page = 0
        self.total_pages = 0
        self.zoom = 1.0
        self.pdf_path: str | None = None
        self.render_dir: str = ""

        # Блокировки
        self._render_lock = threading.Lock()
        self._stop_preload = threading.Event()

        # Для swipe-детекции
        self._drag_start_x = 0
        self._search_timer = None

        # Для истории чтения
        self._read_start_time = time.time()

        # Создаём временную директорию
        os.makedirs(TEMP_DIR, exist_ok=True)
        self.render_dir = tempfile.mkdtemp(dir=TEMP_DIR) or ""

        # Поиск по тексту
        self._search_results = []  # [(page_num, rects), ...]
        self._search_match_index = 0
        self._search_query = ""
        self._search_is_loading = False

        self.search_field = ft.TextField(
            hint_text="Поиск по тексту...",
            height=35, text_size=13,
            expand=True,
            on_submit=lambda e: self._do_search(e),
            on_change=lambda e: self._on_search_change(),
        )
        self.search_match_label = ft.Text("", size=12)
        self.search_panel = ft.Container(
            content=ft.Row([
                ft.Icon(ft.icons.SEARCH, size=18),
                self.search_field,
                self.search_match_label,
                ft.IconButton(icon=ft.icons.NAVIGATE_BEFORE, icon_size=18,
                              tooltip="Предыдущее совпадение", on_click=self._prev_match),
                ft.IconButton(icon=ft.icons.NAVIGATE_NEXT, icon_size=18,
                              tooltip="Следующее совпадение", on_click=self._next_match),
                ft.IconButton(icon=ft.icons.CLOSE, icon_size=18,
                              tooltip="Закрыть поиск", on_click=self._close_search),
            ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.padding.symmetric(horizontal=10, vertical=4),
            bgcolor=ft.colors.SURFACE_VARIANT,
            visible=False,
        )

        # UI - ИЗОБРАЖЕНИЕ (самое важное!)
        self.page_image = ft.Image(
            src="",
            fit=ft.ImageFit.NONE,
            gapless_playback=True,  # Важно для плавной смены!
        )

        # Иконка-заглушка
        self.placeholder = ft.Column(
            [ft.Icon(ft.icons.MENU_BOOK, size=80, color=ft.colors.with_opacity(0.3, ft.colors.ON_SURFACE))],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
        )

        # Контейнер изображения
        self.image_stack = ft.Stack(
            [self.placeholder, self.page_image],
            alignment=ft.alignment.center,
        )

        # Loading
        self.loading_ring = ft.ProgressRing(width=40, height=40)
        self.loading_text = ft.Text("Подготовка...")
        self.progress_bar = ft.ProgressBar(width=200, bar_height=3)

        self.loading_column = ft.Column(
            [self.loading_ring, self.progress_bar, self.loading_text],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=15,
        )

        # Ошибка
        self.error_text = ft.Text("", color=ft.colors.ERROR, text_align=ft.TextAlign.CENTER)
        self.download_btn_big = ft.ElevatedButton(
            "Скачать книгу",
            icon=ft.icons.DOWNLOAD,
            on_click=self._download_book,
            visible=False,
        )

        self.error_column = ft.Column(
            [self.error_text, self.download_btn_big],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=20,
        )

        # Оверлей
        self.overlay = ft.Container(
            content=self.loading_column,
            alignment=ft.alignment.center,
            expand=True,
            bgcolor=ft.colors.SURFACE,
        )

        # Навигация
        self.prev_btn = ft.IconButton(
            icon=ft.icons.ARROW_LEFT,
            on_click=self._prev_page,
            disabled=True,
        )
        self.next_btn = ft.IconButton(
            icon=ft.icons.ARROW_RIGHT,
            on_click=self._next_page,
            disabled=True,
        )
        self.zoom_label = ft.Text(f"{int(self.zoom * 100)}%")
        self.page_input = ft.TextField(
            hint_text="№",
            width=50,
            height=35,
            text_size=13,
            text_align=ft.TextAlign.CENTER,
            on_submit=self._jump_to_page,
        )
        self.total_pages_text = ft.Text(f"{self.total_pages}", size=14, weight=ft.FontWeight.BOLD)

        # Строим UI
        self.content = self._build_ui()

        # Запускаем загрузку
        threading.Thread(target=self._load_pdf, daemon=True).start()

    def _build_ui(self) -> ft.Control:
        top_bar = ft.Container(
            content=ft.Row([
                ft.IconButton(icon=ft.icons.ARROW_BACK, on_click=self._go_back, tooltip="Назад"),
                ft.Text(self.book.title, size=16, weight=ft.FontWeight.BOLD, expand=True, overflow=ft.TextOverflow.ELLIPSIS),
                ft.IconButton(icon=ft.icons.SEARCH, on_click=self._toggle_search, tooltip="Поиск (Ctrl+F)"),
                ft.IconButton(icon=ft.icons.STAR_BORDER, on_click=self._add_bookmark, tooltip="Закладка"),
                ft.IconButton(icon=ft.icons.ZOOM_OUT, on_click=self._zoom_out, tooltip="Уменьшить"),
                self.zoom_label,
                ft.IconButton(icon=ft.icons.ZOOM_IN, on_click=self._zoom_in, tooltip="Увеличить"),
                ft.IconButton(icon=ft.icons.SAVE_ALT, on_click=self._save_page_as_image,
                              tooltip="Сохранить страницу как изображение"),
            ]),
            padding=10,
            bgcolor=ft.colors.SURFACE_VARIANT,
        )

        scrollable_content = ft.GestureDetector(
            content=ft.Column(
                [self.image_stack],
                scroll=ft.ScrollMode.AUTO,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                expand=True,
                on_scroll=self._on_scroll,
            ),
            on_horizontal_drag_start=self._on_drag_start,
            on_horizontal_drag_end=self._on_drag_end,
        )

        viewer = ft.Container(
            content=ft.Stack([scrollable_content, self.overlay], expand=True),
            expand=True,
            alignment=ft.alignment.center,
        )

        bottom_bar = ft.Container(
            content=ft.Row([
                self.prev_btn,
                ft.Container(expand=True),
                self.page_input,
                ft.Text(" / ", size=14),
                self.total_pages_text,
                ft.Container(width=10),
                ft.IconButton(
                    icon=ft.icons.SEND,
                    icon_size=16,
                    tooltip="Перейти",
                    on_click=self._jump_to_page,
                ),
                ft.Container(expand=True),
                self.next_btn,
            ], alignment=ft.MainAxisAlignment.CENTER),
            padding=ft.padding.symmetric(horizontal=10, vertical=5),
            bgcolor=ft.colors.SURFACE_VARIANT,
        )

        return ft.Column([self.search_panel, top_bar, viewer, bottom_bar], expand=True, spacing=0)

    def _show_loading(self, text: str = "Загрузка..."):
        """Показывает индикатор загрузки"""
        self.loading_text.value = text
        self.loading_ring.visible = True
        self.progress_bar.visible = False
        self.overlay.content = self.loading_column
        self.overlay.visible = True
        self.page_image.visible = False
        self.placeholder.visible = False
        try:
            self.page.update()
        except Exception:
            pass

    def _show_error(self, error: str, show_download: bool = False):
        """Показывает ошибку"""
        self.error_text.value = error
        self.download_btn_big.visible = show_download
        self.overlay.content = self.error_column
        self.overlay.visible = True
        self.page_image.visible = False
        try:
            self.page.update()
        except Exception:
            pass

    def _save_progress(self):
        """Сохраняет прогресс чтения в БД"""
        if self.book.id > 0 and self.pdf_doc:
            from src.core.database import Database
            db = Database()
            db.save_reading_progress(self.book.id, self.current_page + 1)

    def _log_reading_event(self, duration_seconds: int):
        """Записывает событие чтения в историю"""
        if self.book.id <= 0:
            return
        try:
            from src.core.database import Database
            Database().add_reading_event(self.book.id, self.current_page + 1, duration_seconds)
        except Exception as e:
            print(f"[PDF] Ошибка записи истории чтения: {e}")

    def _show_page_image(self, image_path: str):
        """Показывает изображение страницы - ГЛАВНЫЙ МЕТОД"""
        try:
            with self._render_lock:
                page = self.pdf_doc[self.current_page] if self.pdf_doc else None
                if page:
                    try:
                        page_size = page.get_size()
                    finally:
                        page.close()
                else:
                    page_size = None
            if page_size:
                scaled_width = max(1, int(page_size[0] * self.zoom))
                scaled_height = max(1, int(page_size[1] * self.zoom))
                self.page_image.width = scaled_width
                self.page_image.height = scaled_height
                self.image_stack.width = scaled_width
                self.image_stack.height = scaled_height

            # Устанавливаем src изображения
            self.page_image.src = image_path
            self.page_image.visible = True
            self.placeholder.visible = False
            self.overlay.visible = False

            # Обновляем счётчик
            self.total_pages_text.value = str(self.total_pages)
            self.page_input.value = str(self.current_page + 1)
            self.prev_btn.disabled = self.current_page <= 0
            self.next_btn.disabled = self.current_page >= self.total_pages - 1

            # Сохраняем прогресс
            self._save_progress()

            # Обновляем страницу
            self.page.update()
            print(f"[PDF] Показана страница {self.current_page + 1}")
        except Exception as e:
            print(f"[PDF] Ошибка показа: {e}")

    def _handle_keyboard(self, e: ft.KeyboardEvent):
        if e.key == "F" and e.ctrl:
            self._toggle_search()
        elif e.key == "Arrow Left" or e.key == "ArrowLeft":
            self._prev_page(e)
        elif e.key == "Arrow Right" or e.key == "ArrowRight":
            self._next_page(e)

    def _load_pdf(self):
        """Загружает PDF в фоне"""
        print(f"[PDF] Загрузка: {self.book.title}")
        self._show_loading("Поиск книги...")

        if not PDFIUM_AVAILABLE:
            self._show_error("❌ pypdfium2 не установлен!\n\npip install pypdfium2")
            return

        try:
            # Ищем файл
            is_downloaded, filepath = self.downloader.is_book_downloaded(self.book)
            if not is_downloaded or not filepath or not os.path.exists(filepath):
                is_downloaded, filepath = self._find_pdf_manual()

            if is_downloaded and filepath and os.path.exists(filepath):
                self.pdf_path = filepath
                print(f"[PDF] Файл найден: {filepath}")
            else:
                # Пробуем офлайн-кэш
                cached = self.downloader.get_cached_pdf(self.book)
                if cached and os.path.exists(cached):
                    self.pdf_path = cached
                    print(f"[PDF] Файл из кэша: {cached}")
                else:
                    self._show_error(
                        "❌ Книга не скачана!\n\nСначала скачайте книгу.",
                        show_download=True
                    )
                    return

            # Копируем в офлайн-кэш в фоне (если книга взята не из кэша)
            if self.pdf_path and not self.downloader.get_cached_pdf(self.book):
                threading.Thread(
                    target=lambda: self.downloader.ensure_cached(self.book, self.pdf_path),
                    daemon=True,
                ).start()

            # Открываем
            self._show_loading("Открытие...")
            self.pdf_doc = pdfium.PdfDocument(self.pdf_path)
            self.total_pages = len(self.pdf_doc)
            print(f"[PDF] Страниц: {self.total_pages}")

            # Определяем начальную страницу (приоритет: go_to_page > сохраненный прогресс > 0)
            if self.go_to_page is not None:
                start_page = self.go_to_page - 1
            else:
                from src.core.database import Database
                saved_page = Database().get_reading_progress(self.book.id)
                start_page = (saved_page - 1) if saved_page else 0
            start_page = max(0, min(start_page, self.total_pages - 1))

            # Рендерим первую страницу и показываем
            first_page_path = self._render_page(start_page)
            if first_page_path:
                self._show_page_image(first_page_path)
                self._log_reading_event(0)
            else:
                self._show_error("Ошибка рендеринга первой страницы")
                return

            # Предзагрузка
            threading.Thread(target=self._preload_pages, daemon=True).start()

        except Exception as e:
            print(f"[PDF] Ошибка: {e}")
            import traceback
            traceback.print_exc()
            self._show_error(f"Ошибка:\n{e}")

    def _find_pdf_manual(self) -> tuple:
        """Ищет PDF вручную"""
        paths = []

        # Downloads
        dl_dir = os.path.join(os.path.expanduser("~/Downloads"), "downloads-nurbooks")
        if os.path.exists(dl_dir):
            for f in os.listdir(dl_dir):
                if f.endswith(".pdf"):
                    paths.append(os.path.join(dl_dir, f))

        # Проект
        base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        for folder in ["saved_books", "pdfs"]:
            p = os.path.join(base, folder)
            if os.path.exists(p):
                for f in os.listdir(p):
                    if f.endswith(".pdf"):
                        paths.append(os.path.join(p, f))

        for path in paths:
            try:
                doc = pdfium.PdfDocument(path)
                count = len(doc)
                doc.close()
                if count > 0:
                    # Проверяем принадлежность книге
                    if str(self.book.id) in path or self.book.title[:15].replace(" ", "_") in path:
                        return True, path
            except Exception as e:
                print(f"[PDF] Ошибка при проверке файла {path}: {e}")
                continue

        return False, None

    def _render_page(self, page_num: int) -> str | None:
        """Рендерит страницу в файл и возвращает путь"""
        if not self.pdf_doc or page_num < 0 or page_num >= self.total_pages:
            return None

        try:
            with self._render_lock:
                # Имя файла включает зум для корректного кэширования
                output_path = os.path.join(self.render_dir, f"page_{page_num}_zoom{int(self.zoom*100)}.png")

                # Если уже есть - возвращаем
                if os.path.exists(output_path):
                    return output_path

                # Рендерим
                page = self.pdf_doc[page_num]
                bitmap = page.render(scale=self.zoom, rev_byteorder=True)
                try:
                    _save_bitmap_png(bitmap, output_path)
                finally:
                    bitmap.close()
                    page.close()

                print(f"[PDF] Страница {page_num + 1} отрендерена (zoom={self.zoom})")
                return output_path

        except Exception as e:
            print(f"[PDF] Ошибка рендеринга: {e}")
            return None

    def _preload_pages(self):
        """Предзагрузка соседних страниц"""
        time.sleep(0.5)

        offsets = [1, 2, -1, 3, 4]
        for offset in offsets:
            if self._stop_preload.is_set():
                break
            pg = self.current_page + offset
            if 0 <= pg < self.total_pages:
                self._render_page(pg)

    def _show_page(self, page_num: int):
        """Показывает страницу (вызывается из потока)"""
        if page_num < 0 or page_num >= self.total_pages:
            return

        self.current_page = page_num

        # Пытаемся найти в кэше
        path = self._render_page(page_num)
        if path:
            self._show_page_image(path)
            return

        # Ошибка
        self._show_error("Ошибка рендеринга")

    def _prev_page(self, e):
        if self.current_page > 0:
            self._show_loading(f"Страница {self.current_page}...")
            threading.Thread(target=self._show_page, args=(self.current_page - 1,), daemon=True).start()
            threading.Thread(target=self._preload_pages, daemon=True).start()

    def _next_page(self, e):
        if self.current_page < self.total_pages - 1:
            self._show_loading(f"Страница {self.current_page + 2}...")
            threading.Thread(target=self._show_page, args=(self.current_page + 1,), daemon=True).start()
            threading.Thread(target=self._preload_pages, daemon=True).start()

    def _on_scroll(self, e: ft.OnScrollEvent):
        if e.event_type == "user" and e.scroll_delta:
            if e.direction == "left" and abs(e.scroll_delta) > 30:
                self._next_page(None)
            elif e.direction == "right" and abs(e.scroll_delta) > 30:
                self._prev_page(None)

    def _on_drag_start(self, e):
        self._drag_start_x = getattr(e, "global_x", 0) or 0

    def _on_drag_end(self, e):
        end_x = getattr(e, "global_x", 0) or 0
        if end_x is not None and self._drag_start_x is not None:
            delta = end_x - self._drag_start_x
            if delta > 80 and self._drag_start_x < 60:
                self._go_back(e)

    def _zoom_in(self, e):
        if self.zoom < 3.0:
            self.zoom = round(self.zoom + 0.25, 2)
            self._apply_zoom()

    def _zoom_out(self, e):
        if self.zoom > 0.5:
            self.zoom = round(self.zoom - 0.25, 2)
            self._apply_zoom()

    def _apply_zoom(self):
        """Применяет новый зум"""
        # Обновляем метку
        self.zoom_label.value = f"{int(self.zoom * 100)}%"
        # Показываем loading
        self._show_loading(f"Масштаб {int(self.zoom * 100)}%...")
        # Рендерим в фоне и показываем
        def render_zoomed():
            path = self._render_page(self.current_page)
            if path:
                self._show_page_image(path)
            else:
                self._show_error("Ошибка изменения масштаба")
        threading.Thread(target=render_zoomed, daemon=True).start()

    def _clear_render_cache(self):
        """Очищает кэш рендеринга"""
        try:
            if self.render_dir and os.path.exists(self.render_dir):
                for f in os.listdir(self.render_dir):
                    os.remove(os.path.join(self.render_dir, f))
        except Exception:
            pass

    def _download_book(self, e):
        """Скачивает книгу"""
        def download():
            try:
                self._show_loading("Скачивание...")
                path = self.downloader.download_book(self.book)
                from src.core.statistics_manager import stats
                stats.increment_download_count(self.book.id)
                self.book.download_count = getattr(self.book, "download_count", 0) + 1
                print(f"[PDF] Скачано: {path}")

                # Копируем в офлайн-кэш
                self.downloader.ensure_cached(self.book, path)

                # Показываем уведомление
                self.page.snack_bar = ft.SnackBar(ft.Text(f"Скачано: {self.book.title}"))
                self.page.snack_bar.open = True
                self.page.update()

                # Перезагружаем
                if self.pdf_doc:
                    self.pdf_doc.close()

                self._load_pdf()
            except Exception as ex:
                self._show_error(f"Ошибка скачивания:\n{ex}")
        threading.Thread(target=download, daemon=True).start()

    def _save_page_as_image(self, e):
        """Сохраняет текущую страницу как PNG в папку загрузок"""
        if not self.pdf_doc:
            return
        def save():
            try:
                path = self._render_page(self.current_page)
                if not path:
                    raise Exception("Не удалось отрендерить страницу")
                save_name = f"{self.book.title}_page_{self.current_page + 1}.png"
                safe_name = "".join(c for c in save_name if c.isalnum() or c in " ._-")
                save_dir = os.path.join(os.path.expanduser("~"), "Downloads")
                save_path = os.path.join(save_dir, safe_name)
                import shutil
                shutil.copy2(path, save_path)
                self.page.snack_bar = ft.SnackBar(
                    content=ft.Text(f"Страница сохранена: {safe_name}"),
                    action="OK",
                    duration=3000,
                )
                self.page.snack_bar.open = True
                self.page.update()
            except Exception as ex:
                self.page.snack_bar = ft.SnackBar(
                    content=ft.Text(f"Ошибка: {ex}"),
                    bgcolor=ft.colors.ERROR_CONTAINER,
                )
                self.page.snack_bar.open = True
                self.page.update()
        threading.Thread(target=save, daemon=True).start()

    def _go_back(self, e):
        """Возврат"""
        self._stop_preload.set()
        self._save_progress()
        self._log_reading_event(int(time.time() - self._read_start_time))
        self._close_search()
        # Восстанавливаем предыдущий обработчик клавиатуры
        self.page.on_keyboard_event = self._prev_keyboard
        if self.pdf_doc:
            try:
                self.pdf_doc.close()
            except Exception:
                pass
        self._clear_render_cache()
        if self.on_back:
            self.on_back()

    def _add_bookmark(self, e):
        """Добавляет закладку для текущей страницы"""
        from datetime import datetime

        from src.core.database import Database

        db = Database()

        # Проверяем, нет ли уже закладки на этой странице
        existing = [b for b in self.bookmarks if b.book_id == self.book.id and b.page_number == self.current_page + 1]
        if existing:
            self.page.snack_bar = ft.SnackBar(
                content=ft.Text(f"Закладка на странице {self.current_page + 1} уже существует", color=ft.colors.BLACK),
                bgcolor=ft.colors.AMBER
            )
            self.page.snack_bar.open = True
            self.page.update()
            return

        # Добавляем закладку
        from src.core.models import Bookmark
        bookmark = Bookmark(
            id=0,
            book_id=self.book.id,
            page_number=self.current_page + 1,
            timestamp=datetime.now().isoformat()
        )

        if db.add_bookmark(bookmark):
            self.bookmarks.append(bookmark)
            self.page.snack_bar = ft.SnackBar(
                content=ft.Text(f"Закладка добавлена на страницу {self.current_page + 1}"),
                bgcolor=ft.colors.GREEN
            )
        else:
            self.page.snack_bar = ft.SnackBar(
                content=ft.Text("Ошибка при добавлении закладки"),
                bgcolor=ft.colors.ERROR
            )
        self.page.snack_bar.open = True
        self.page.update()

    def _jump_to_page(self, e):
        """Переход на указанную страницу"""
        try:
            page = int(self.page_input.value)
            if page < 1 or page > self.total_pages:
                self.page.snack_bar = ft.SnackBar(
                    content=ft.Text(f"Страницы {page} нет. Введите от 1 до {self.total_pages}"),
                    bgcolor=ft.colors.ERROR
                )
                self.page.snack_bar.open = True
                self.page.update()
                return
            self._show_loading(f"Страница {page}...")
            threading.Thread(target=self._show_page, args=(page - 1,), daemon=True).start()
            threading.Thread(target=self._preload_pages, daemon=True).start()
        except ValueError:
            self.page_input.value = str(self.current_page + 1)
            self.page.update()

    def _on_search_change(self):
        if self._search_timer:
            self._search_timer.cancel()
        self._search_timer = threading.Timer(0.4, self._do_search)
        self._search_timer.start()

    def _toggle_search(self, e=None):
        self.search_panel.visible = not self.search_panel.visible
        if self.search_panel.visible:
            self.search_field.focus()
        else:
            self._close_search(e)
        self.page.update()

    def _do_search(self, e=None):
        q = self.search_field.value.strip() if self.search_field.value else ""
        if not q or self._search_is_loading:
            if not q:
                self._search_results = []
                self.search_match_label.value = ""
                self.page.update()
            return

        self._search_query = q.lower()
        self._search_is_loading = True
        self.search_match_label.value = "Поиск..."
        self.page.update()

        threading.Thread(target=self._search_worker, daemon=True).start()

    def _search_worker(self):
        results = []
        if not self.pdf_doc:
            self._search_is_loading = False
            return

        for pg in range(self.total_pages):
            try:
                with self._render_lock:
                    page = self.pdf_doc[pg]
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_bounded() or ""
                    finally:
                        textpage.close()
                        page.close()
                if self._search_query in text.lower():
                    found = text.lower().count(self._search_query)
                    results.append((pg, found))
            except Exception:
                continue

        self._search_results = results
        self._search_match_index = 0
        self._search_is_loading = False

        total = sum(r[1] for r in results)
        pages = len(results)
        self.search_match_label.value = f"{total} совп. на {pages} стр." if total else "Нет совпадений"

        if results:
            target_page = results[0][0]
            if target_page != self.current_page:
                self._show_loading("Переход к результату...")
                threading.Thread(target=self._show_page, args=(target_page,), daemon=True).start()
        self.page.update()

    def _next_match(self, e):
        if not self._search_results:
            return
        self._search_match_index += 1
        if self._search_match_index >= len(self._search_results):
            self._search_match_index = 0
        target_page = self._search_results[self._search_match_index][0]
        if target_page != self.current_page:
            threading.Thread(target=self._show_page, args=(target_page,), daemon=True).start()
        self.page.update()

    def _prev_match(self, e):
        if not self._search_results:
            return
        self._search_match_index -= 1
        if self._search_match_index < 0:
            self._search_match_index = len(self._search_results) - 1
        target_page = self._search_results[self._search_match_index][0]
        if target_page != self.current_page:
            threading.Thread(target=self._show_page, args=(target_page,), daemon=True).start()
        self.page.update()

    def _close_search(self, e=None):
        self.search_panel.visible = False
        self._search_results = []
        self._search_query = ""
        self._search_is_loading = False
        if self._search_timer:
            self._search_timer.cancel()
            self._search_timer = None
        self.search_field.value = ""
        self.search_match_label.value = ""
        if self.page:
            self.page.update()

    def build(self) -> ft.Control:
        return self.content


def on_app_exit():
    """Вызывать при выходе из приложения"""
    cleanup_temp_files()

