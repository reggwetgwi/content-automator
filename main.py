# -*- coding: utf-8 -*-
"""
main.py — объединённое GUI-приложение для творческой автоматизации
обработки изображений.

Конвейер (do_work):
    1. Взять базовый референс из ЛОКАЛЬНОЙ папки ./references/
       (изображения, которыми вы владеете или которые лицензированы).
    2. Выбрать случайный эталон из ./source_images/.
    3. Объединить их через Wavespeed (ваш класс ImageEditor).
    4. Сохранить результат в ./output/.
    5. Подготовить пост (изображение + подпись) к публикации.

ВАЖНО про публикацию:
    Автопостинг через неофициальный API (instagrapi) нарушает правила
    Instagram и ведёт к блокировкам. Поэтому здесь конвейер сохраняет
    готовый пост и подпись в ./output/, а для автоматической публикации
    оставлен документированный хук под ОФИЦИАЛЬНЫЙ Instagram Graph API
    (он легально поддерживает запланированную публикацию для бизнес- и
    creator-аккаунтов). См. функцию publish_official() ниже.

Аналогично источник референсов — это локальная папка с вашими
материалами, а не автоматический сбор чужих публикаций.

Все пути создаются автоматически. Ключи — из .env.
"""

import os
import sys
import json
import queue
import random
import logging
import threading
from pathlib import Path
from datetime import datetime

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

from dotenv import load_dotenv

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Загружаем .env (WAVESPEED_API_KEY, при необходимости IG_*).
load_dotenv()

# ----------------------------------------------------------------------
# Пути проекта — создаются автоматически при старте.
# ----------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
REFERENCES_DIR = BASE_DIR / "references"      # ваши базовые референсы (вместо скрапинга)
SOURCE_IMAGES_DIR = BASE_DIR / "source_images"  # ваши эталонные изображения
SPENT_SOURCES_DIR = SOURCE_IMAGES_DIR / "_spent"  # могильник отработанных сорсов
OUTPUT_DIR = BASE_DIR / "output"              # результаты + подготовленные посты
LOGS_DIR = BASE_DIR / "logs"
CONFIG_FILE = BASE_DIR / "config.json"

for _d in (REFERENCES_DIR, SOURCE_IMAGES_DIR, SPENT_SOURCES_DIR, OUTPUT_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Стоимость одного запроса к Wavespeed, $ (для оценки расхода).
COST_PER_OP = 0.03

DEFAULT_CONFIG = {
    "interval": 30,     # интервал запуска конвейера, сек
    "limit": 5,         # лимит успешных операций
    "test_mode": True,  # тестовый режим: без публикации
    "caption": "Создано в рамках творческого проекта.",
    "prompt": "",       # промпт для image-editing (вписывается в GUI)
    "reference": "",    # имя выбранного референса из ./references/
}

# ----------------------------------------------------------------------
# Настройка файлового логирования (в дополнение к GUI-логу).
# ----------------------------------------------------------------------
logger = logging.getLogger("pipeline")
logger.setLevel(logging.INFO)
_file_handler = logging.FileHandler(
    LOGS_DIR / f"app_{datetime.now():%Y%m%d}.log", encoding="utf-8"
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(message)s")
)
logger.addHandler(_file_handler)


# ======================================================================
# Импорт вашего обработчика изображений (Блок 3).
# Логику не меняем — используем как есть. Если модуль недоступен,
# показываем понятную ошибку при попытке обработки.
# ======================================================================
try:
    from image_editor import ImageEditor  # ваш класс с методом merge()
    _IMAGE_EDITOR_AVAILABLE = True
except Exception as _e:  # noqa: BLE001 — фиксируем причину для лога
    ImageEditor = None
    _IMAGE_EDITOR_AVAILABLE = False
    _IMPORT_ERROR = _e


# ======================================================================
# Источник референсов — ЛОКАЛЬНАЯ папка (замена автоскрапингу).
# ======================================================================
class LocalReferenceSource:
    """Отдаёт путь к случайному базовому референсу из ./references/.

    Это осознанная замена сбору чужих публикаций: вы кладёте в папку
    только те изображения, на которые у вас есть права.
    """

    SUPPORTED = {".jpg", ".jpeg", ".png", ".webp"}

    def __init__(self, folder: Path):
        self.folder = folder

    def get_image(self):
        """Вернуть путь к случайному файлу или None, если папка пуста."""
        files = self.list_images()
        if not files:
            return None
        return random.choice(files)

    def list_images(self):
        """Список путей ко всем изображениям в папке (отсортирован по имени)."""
        files = [
            p for p in self.folder.iterdir()
            if p.suffix.lower() in self.SUPPORTED and p.is_file()
        ]
        return sorted(files, key=lambda p: p.name.lower())

    def get_by_name(self, name):
        """Вернуть путь к референсу по имени файла или None, если нет."""
        path = self.folder / name
        if path.is_file() and path.suffix.lower() in self.SUPPORTED:
            return path
        return None


# ======================================================================
# Подготовка/публикация поста.
# ======================================================================
def export_post(image_path: Path, caption: str) -> Path:
    """Сохранить готовый пост: рядом с изображением кладём .txt с подписью.

    Так пост можно опубликовать вручную или через официальный API.
    """
    caption_path = image_path.with_suffix(image_path.suffix + ".caption.txt")
    caption_path.write_text(caption, encoding="utf-8")
    return caption_path


def publish_official(image_path: Path, caption: str):
    """ХУК под официальный Instagram Graph API (легальный автопостинг).

    Намеренно не реализован «из коробки», т.к. требует:
      * бизнес-/creator-аккаунт, привязанный к Facebook-странице;
      * долгоживущий access token и IG user id;
      * публично доступный URL изображения (Graph API не принимает
        локальный файл напрямую — нужен hosted URL).

    Документация: developers.facebook.com → Instagram Graph API →
    Content Publishing. Реализуйте здесь два шага:
      1) POST /{ig-user-id}/media       (создать контейнер)
      2) POST /{ig-user-id}/media_publish (опубликовать контейнер)
    """
    raise NotImplementedError(
        "Официальная публикация не настроена. Пост сохранён в ./output/ "
        "для ручной выкладки. Подключите Instagram Graph API в publish_official()."
    )


# ======================================================================
# Главное приложение.
# ======================================================================
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Творческая автоматизация контента")
        self.root.geometry("640x560")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # --- Состояние ---
        self.scheduler = BackgroundScheduler()
        self.job = None
        self.job_id = "pipeline_job"
        self.success_count = 0          # успешные операции (для счётчика и расхода)
        self.counter_lock = threading.Lock()
        self.log_queue = queue.Queue()  # потокобезопасная передача логов в GUI

        # Источник референсов и (опционально) обработчик.
        self.ref_source = LocalReferenceSource(REFERENCES_DIR)
        self.editor = None  # ленивая инициализация ImageEditor при первом запуске

        self._build_ui()
        self.load_config()
        self.scheduler.start()
        self._poll_log_queue()

        # Подсказка по пустым папкам.
        self._startup_hints()

    # ------------------------------------------------------------------
    # Интерфейс
    # ------------------------------------------------------------------
    def _build_ui(self):
        # Настройки.
        sf = ttk.LabelFrame(self.root, text="Настройки")
        sf.pack(fill="x", padx=10, pady=8)

        ttk.Label(sf, text="Интервал (сек):").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.interval_var = tk.StringVar()
        ttk.Entry(sf, textvariable=self.interval_var, width=8).grid(row=0, column=1, padx=5)

        ttk.Label(sf, text="Лимит:").grid(row=0, column=2, sticky="w", padx=5)
        self.limit_var = tk.StringVar()
        ttk.Entry(sf, textvariable=self.limit_var, width=8).grid(row=0, column=3, padx=5)

        self.test_var = tk.BooleanVar()
        ttk.Checkbutton(sf, text="Тестовый режим (без публикации)",
                        variable=self.test_var).grid(row=1, column=0, columnspan=3,
                                                     sticky="w", padx=5, pady=3)

        ttk.Label(sf, text="Подпись:").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        self.caption_var = tk.StringVar()
        ttk.Entry(sf, textvariable=self.caption_var, width=55).grid(
            row=2, column=1, columnspan=3, sticky="w", padx=5)

        # Промпт для image-editing — вписывается вручную, не зашит в код.
        ttk.Label(sf, text="Промпт:").grid(row=3, column=0, sticky="nw", padx=5, pady=5)
        self.prompt_text = tk.Text(sf, width=55, height=4, wrap="word")
        self.prompt_text.grid(row=3, column=1, columnspan=3, sticky="w", padx=5, pady=5)

        # Выбор референса из ./references/ — какой из 4 (или скольких есть) брать.
        ttk.Label(sf, text="Референс:").grid(row=4, column=0, sticky="w", padx=5, pady=5)
        self.reference_var = tk.StringVar()
        self.reference_combo = ttk.Combobox(
            sf, textvariable=self.reference_var, width=40, state="readonly"
        )
        self.reference_combo.grid(row=4, column=1, columnspan=2, sticky="w", padx=5)
        ttk.Button(sf, text="Обновить", command=self._refresh_references).grid(
            row=4, column=3, sticky="w", padx=5)

        # Кнопки.
        bf = ttk.Frame(self.root)
        bf.pack(fill="x", padx=10, pady=4)
        self.start_btn = ttk.Button(bf, text="Старт", command=self.on_start)
        self.start_btn.pack(side="left", padx=4)
        self.pause_btn = ttk.Button(bf, text="Пауза", command=self.on_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(bf, text="Стоп", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.test_btn = ttk.Button(bf, text="Тест (1 цикл)", command=self.on_test)
        self.test_btn.pack(side="left", padx=12)

        # Индикаторы: статус, счётчик, расход.
        inf = ttk.Frame(self.root)
        inf.pack(fill="x", padx=12, pady=2)
        self.status_var = tk.StringVar(value="Остановлено")
        self.count_var = tk.StringVar(value="Успешно: 0")
        self.cost_var = tk.StringVar(value="Расход: $0.00")
        ttk.Label(inf, textvariable=self.status_var, foreground="gray").pack(side="left")
        ttk.Label(inf, textvariable=self.count_var).pack(side="left", padx=20)
        ttk.Label(inf, textvariable=self.cost_var).pack(side="left")

        # Лог.
        lf = ttk.LabelFrame(self.root, text="Лог")
        lf.pack(fill="both", expand=True, padx=10, pady=8)
        self.log_text = scrolledtext.ScrolledText(lf, height=14, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

    # ------------------------------------------------------------------
    # Логирование (GUI + файл)
    # ------------------------------------------------------------------
    def log(self, message, level=logging.INFO):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {message}")
        logger.log(level, message)

    def _poll_log_queue(self):
        while not self.log_queue.empty():
            line = self.log_queue.get_nowait()
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(100, self._poll_log_queue)

    def _startup_hints(self):
        if not any(REFERENCES_DIR.iterdir()):
            self.log("Папка ./references/ пуста — добавьте свои базовые изображения.")
        if not any(SOURCE_IMAGES_DIR.iterdir()):
            self.log("Папка ./source_images/ пуста — добавьте эталонные изображения.")
        if not _IMAGE_EDITOR_AVAILABLE:
            self.log("Модуль image_editor не найден — обработка будет недоступна "
                     f"({_IMPORT_ERROR}).", level=logging.WARNING)

    def _refresh_references(self, select=None):
        """Перечитать ./references/ и обновить выпадающий список.

        select — имя файла, которое нужно выбрать после обновления
        (например, сохранённое в config). Если его нет — берём первый.
        """
        names = [p.name for p in self.ref_source.list_images()]
        self.reference_combo["values"] = names
        if not names:
            self.reference_var.set("")
            self.log("В ./references/ нет изображений.", logging.WARNING)
            return
        # Сохраняем текущий выбор, если он ещё валиден.
        current = select or self.reference_var.get()
        self.reference_var.set(current if current in names else names[0])

    # ------------------------------------------------------------------
    # Конвейер
    # ------------------------------------------------------------------
    def _ensure_editor(self):
        """Ленивая инициализация ImageEditor (Блок 3)."""
        if self.editor is None:
            if not _IMAGE_EDITOR_AVAILABLE:
                raise RuntimeError("image_editor недоступен.")
            self.editor = ImageEditor()  # логику класса не трогаем
        return self.editor

    def do_work(self, test_only=False):
        """Один проход конвейера. Запускается планировщиком в отдельном потоке."""
        # Проверка лимита (только для планового режима).
        if not test_only:
            with self.counter_lock:
                limit = self._int(self.limit_var.get(), DEFAULT_CONFIG["limit"])
                if self.success_count >= limit:
                    self.log(f"Достигнут лимит ({limit}). Остановка.")
                    self.root.after(0, self._limit_reached)
                    return

        try:
            # 1. Референс — выбранный в выпадающем списке (не случайный).
            self.log("Этап 1/4: выбор референса…")
            ref_name = self.reference_var.get()
            ref_path = self.ref_source.get_by_name(ref_name) if ref_name else None
            if ref_path is None:
                self.log("Референс не выбран или файл отсутствует — выберите "
                         "референс в списке. Пропуск цикла.", logging.WARNING)
                return

            # 2. Случайный ещё не отработанный сорс.
            # iterdir() не заходит в подпапки, но папку _spent отфильтруем явно.
            self.log("Этап 2/4: выбор эталона из ./source_images/…")
            sources = [p for p in SOURCE_IMAGES_DIR.iterdir()
                       if p.is_file()
                       and p.suffix.lower() in LocalReferenceSource.SUPPORTED]
            if not sources:
                self.log("Нет новых сорсов в ./source_images/ — подкинь файлы. "
                         "Пропуск цикла.", logging.WARNING)
                return
            target_path = random.choice(sources)

            # 3. Обработка через Wavespeed (ваш ImageEditor.merge).
            # Промпт берём из поля GUI — он больше не зашит в код.
            prompt = self.prompt_text.get("1.0", "end").strip()
            if not prompt:
                self.log("Промпт пуст — впишите текст в поле «Промпт». Пропуск цикла.",
                         logging.WARNING)
                return
            self.log(f"Этап 3/4: обработка ({ref_path.name} + {target_path.name})…")
            editor = self._ensure_editor()
            # Порядок важен: референс (./references/) идёт первым в images[0],
            # source-эталон (./source_images/) — вторым. Промпт из GUI.
            result = editor.merge(str(ref_path), str(target_path), prompt)

            # Считаем операцию выполненной (учёт расхода).
            self.root.after(0, self._register_op)

            # 4. Сохранение результата.
            self.log("Этап 4/4: сохранение результата…")
            out_path = self._save_result(result)
            self.log(f"Сохранено: {out_path.name}")

            # Сорс отработан — отправляем в могильник, чтобы не взять повторно.
            # В тестовом цикле НЕ перемещаем: тест должен быть повторяемым.
            if not test_only:
                self._spend_source(target_path)

            # Публикация (если не тест и не тестовый режим).
            if test_only or self.test_var.get():
                self.log("Тест/без публикации: пост сохранён, выкладка пропущена.")
            else:
                caption = self.caption_var.get()
                export_post(out_path, caption)
                self.log("Пост подготовлен в ./output/. Публикация — вручную "
                         "или через официальный API (publish_official).")
                # При желании: раскомментируйте, реализовав publish_official().
                # publish_official(out_path, caption)

            # Учёт успешной операции (планируемый режим).
            if not test_only:
                with self.counter_lock:
                    self.success_count_for_limit = getattr(self, "success_count_for_limit", 0)
                self._bump_success()

        except NotImplementedError as e:
            self.log(f"Публикация не настроена: {e}", logging.WARNING)
        except Exception as e:  # noqa: BLE001
            self.log(f"Ошибка в конвейере: {e}", logging.ERROR)

    def _save_result(self, result) -> Path:
        """Сохранить результат обработки в ./output/.

        result может быть URL (str) или байтами — поддерживаем оба варианта.
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = OUTPUT_DIR / f"result_{ts}.jpg"

        if isinstance(result, (bytes, bytearray)):
            out_path.write_bytes(result)
        elif isinstance(result, str) and result.startswith("http"):
            import requests  # локальный импорт — нужен только здесь
            resp = requests.get(result, timeout=60)
            resp.raise_for_status()
            out_path.write_bytes(resp.content)
        else:
            # На случай иного формата — сохраняем как текст-ссылку.
            out_path = out_path.with_suffix(".txt")
            out_path.write_text(str(result), encoding="utf-8")
        return out_path

    def _spend_source(self, src_path: Path):
        """Перенести отработанный сорс в source_images/_spent/.

        Если файл с таким именем там уже есть — добавляем суффикс,
        чтобы не затереть. Ошибки переноса не валят весь цикл.
        """
        try:
            dest = SPENT_SOURCES_DIR / src_path.name
            # Разрешаем коллизию имён: name.jpg -> name_1.jpg, name_2.jpg ...
            counter = 1
            while dest.exists():
                dest = SPENT_SOURCES_DIR / f"{src_path.stem}_{counter}{src_path.suffix}"
                counter += 1
            src_path.replace(dest)  # атомарный перенос в пределах одного диска
            self.log(f"Сорс отработан → _spent/{dest.name}")
        except OSError as e:
            self.log(f"Не удалось перенести сорс в _spent: {e}", logging.WARNING)

    # ------------------------------------------------------------------
    # Обновление счётчиков (в GUI-потоке)
    # ------------------------------------------------------------------
    def _register_op(self):
        """Учёт операции Wavespeed для расчёта расхода."""
        self._ops = getattr(self, "_ops", 0) + 1
        self.cost_var.set(f"Расход: ${self._ops * COST_PER_OP:.2f}")

    def _bump_success(self):
        with self.counter_lock:
            self.success_count += 1
            cur = self.success_count
        self.root.after(0, lambda: self.count_var.set(f"Успешно: {cur}"))

    def _limit_reached(self):
        self.on_stop()
        messagebox.showinfo("Готово", "Достигнут лимит операций. Конвейер остановлен.")

    # ------------------------------------------------------------------
    # Кнопки
    # ------------------------------------------------------------------
    def on_start(self):
        interval = self._int(self.interval_var.get(), 0)
        limit = self._int(self.limit_var.get(), 0)
        if interval <= 0 or limit <= 0:
            messagebox.showerror("Ошибка", "Интервал и лимит должны быть > 0.")
            return

        if self.job is None:
            with self.counter_lock:
                self.success_count = 0
            self.count_var.set("Успешно: 0")
            self.job = self.scheduler.add_job(
                self.do_work, trigger=IntervalTrigger(seconds=interval),
                id=self.job_id, max_instances=1, replace_existing=True,
            )
            self.log(f"Старт. Интервал {interval}с, лимит {limit}.")
        else:
            self.job.resume()
            self.log("Возобновлено.")

        self.status_var.set("Работает")
        self._buttons(start=False, pause=True, stop=True)

    def on_pause(self):
        if self.job is not None:
            self.job.pause()
            self.log("Пауза.")
            self.status_var.set("Пауза")
            self._buttons(start=True, pause=False, stop=True)

    def on_stop(self):
        if self.job is not None:
            try:
                self.scheduler.remove_job(self.job_id)
            except Exception:  # noqa: BLE001
                pass
            self.job = None
            self.log("Остановлено.")
        self.status_var.set("Остановлено")
        self._buttons(start=True, pause=False, stop=False)

    def on_test(self):
        """Один цикл без публикации — в отдельном потоке, чтобы не вешать GUI."""
        self.log("Запуск тестового цикла…")
        threading.Thread(target=self.do_work, kwargs={"test_only": True},
                         daemon=True).start()

    def _buttons(self, start, pause, stop):
        self.start_btn.configure(state="normal" if start else "disabled")
        self.pause_btn.configure(state="normal" if pause else "disabled")
        self.stop_btn.configure(state="normal" if stop else "disabled")

    # ------------------------------------------------------------------
    # config.json
    # ------------------------------------------------------------------
    def load_config(self):
        cfg = dict(DEFAULT_CONFIG)
        if CONFIG_FILE.exists():
            try:
                cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError) as e:
                self.log(f"Не удалось прочитать config.json: {e}", logging.WARNING)
        self.interval_var.set(str(cfg["interval"]))
        self.limit_var.set(str(cfg["limit"]))
        self.test_var.set(bool(cfg["test_mode"]))
        self.caption_var.set(cfg["caption"])
        # Загружаем сохранённый промпт в текстовое поле.
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", cfg.get("prompt", ""))
        # Наполняем список референсов и выбираем сохранённый.
        self._refresh_references(select=cfg.get("reference", ""))

    def save_config(self):
        cfg = {
            "interval": self._int(self.interval_var.get(), DEFAULT_CONFIG["interval"]),
            "limit": self._int(self.limit_var.get(), DEFAULT_CONFIG["limit"]),
            "test_mode": bool(self.test_var.get()),
            "caption": self.caption_var.get(),
            "prompt": self.prompt_text.get("1.0", "end").strip(),
            "reference": self.reference_var.get(),
        }
        try:
            CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        except OSError as e:
            self.log(f"Не удалось сохранить config.json: {e}", logging.WARNING)

    # ------------------------------------------------------------------
    @staticmethod
    def _int(value, default):
        try:
            return int(str(value).strip())
        except (ValueError, TypeError):
            return default

    def on_close(self):
        self.save_config()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
