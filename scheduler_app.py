# -*- coding: utf-8 -*-
"""
Приложение-планировщик на Tkinter + APScheduler.

Возможности:
  * Кнопки Старт / Пауза / Стоп для управления планировщиком.
  * Поля для интервала (в секундах) и лимита постов.
  * Текстовый лог событий.
  * Сохранение/загрузка настроек в config.json.
  * Полезная работа do_work() выполняется в отдельном потоке,
    чтобы не блокировать интерфейс.
"""

import json
import os
import threading
import queue
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime

# APScheduler — фоновый планировщик, работающий в собственных потоках.
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger


# Файл с настройками лежит рядом со скриптом.
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# Значения по умолчанию, если config.json отсутствует или повреждён.
DEFAULT_CONFIG = {
    "interval": 10,   # интервал запуска задачи, секунды
    "limit": 5,       # максимальное число постов (запусков do_work)
}


class SchedulerApp:
    """Главное окно приложения и вся его логика."""

    def __init__(self, root):
        self.root = root
        self.root.title("Планировщик постов")
        self.root.geometry("520x420")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)  # перехват закрытия окна

        # --- Состояние ---
        self.scheduler = BackgroundScheduler()
        self.job = None                 # ссылка на запланированную задачу
        self.job_id = "do_work_job"     # идентификатор задачи в планировщике
        self.posts_done = 0             # счётчик выполненных постов
        self.posts_lock = threading.Lock()  # защита счётчика от гонок между потоками

        # Очередь сообщений: фоновые потоки кладут строки сюда,
        # а GUI-поток периодически забирает их и пишет в лог.
        # Напрямую трогать виджеты Tkinter из чужих потоков нельзя.
        self.log_queue = queue.Queue()

        # --- Построение интерфейса ---
        self._build_ui()

        # --- Загрузка настроек из файла ---
        self.load_config()

        # Запускаем планировщик (пока без задач — они добавятся по «Старт»).
        self.scheduler.start()

        # Запускаем периодическую перекачку лога из очереди в текстовое поле.
        self._poll_log_queue()

    # ------------------------------------------------------------------
    # Построение виджетов
    # ------------------------------------------------------------------
    def _build_ui(self):
        # Рамка с полями ввода настроек.
        settings_frame = ttk.LabelFrame(self.root, text="Настройки")
        settings_frame.pack(fill="x", padx=10, pady=8)

        # Поле «Интервал, сек».
        ttk.Label(settings_frame, text="Интервал (сек):").grid(
            row=0, column=0, sticky="w", padx=5, pady=5
        )
        self.interval_var = tk.StringVar()
        ttk.Entry(settings_frame, textvariable=self.interval_var, width=10).grid(
            row=0, column=1, sticky="w", padx=5, pady=5
        )

        # Поле «Лимит постов».
        ttk.Label(settings_frame, text="Лимит постов:").grid(
            row=0, column=2, sticky="w", padx=5, pady=5
        )
        self.limit_var = tk.StringVar()
        ttk.Entry(settings_frame, textvariable=self.limit_var, width=10).grid(
            row=0, column=3, sticky="w", padx=5, pady=5
        )

        # Рамка с кнопками управления.
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", padx=10, pady=4)

        self.start_btn = ttk.Button(btn_frame, text="Старт", command=self.on_start)
        self.start_btn.pack(side="left", padx=4)

        self.pause_btn = ttk.Button(
            btn_frame, text="Пауза", command=self.on_pause, state="disabled"
        )
        self.pause_btn.pack(side="left", padx=4)

        self.stop_btn = ttk.Button(
            btn_frame, text="Стоп", command=self.on_stop, state="disabled"
        )
        self.stop_btn.pack(side="left", padx=4)

        # Строка состояния (текущий статус + счётчик постов).
        self.status_var = tk.StringVar(value="Остановлено")
        ttk.Label(self.root, textvariable=self.status_var, foreground="gray").pack(
            anchor="w", padx=12
        )

        # Текстовый лог с прокруткой.
        log_frame = ttk.LabelFrame(self.root, text="Лог")
        log_frame.pack(fill="both", expand=True, padx=10, pady=8)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=12, state="disabled", wrap="word"
        )
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

    # ------------------------------------------------------------------
    # Логирование
    # ------------------------------------------------------------------
    def log(self, message):
        """Положить сообщение в очередь (можно вызывать из любого потока)."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {message}")

    def _poll_log_queue(self):
        """Каждые 100 мс перекладываем строки из очереди в виджет лога.

        Метод выполняется в GUI-потоке (через root.after), поэтому
        обращаться к виджетам здесь безопасно.
        """
        while not self.log_queue.empty():
            line = self.log_queue.get_nowait()
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")            # автопрокрутка вниз
            self.log_text.configure(state="disabled")
        self.root.after(100, self._poll_log_queue)

    # ------------------------------------------------------------------
    # Полезная работа
    # ------------------------------------------------------------------
    def do_work(self):
        """Запускается планировщиком в отдельном потоке.

        Здесь должна быть реальная логика (например, публикация поста).
        Сейчас — заглушка с проверкой лимита.
        """
        with self.posts_lock:
            # Проверяем лимит перед выполнением.
            limit = self._get_int(self.limit_var.get(), DEFAULT_CONFIG["limit"])
            if self.posts_done >= limit:
                self.log(f"Достигнут лимит постов ({limit}). Останавливаю планировщик.")
                # Останавливаем задачу из GUI-потока, чтобы не трогать
                # планировщик из его же рабочего потока напрямую.
                self.root.after(0, self.on_stop)
                return
            self.posts_done += 1
            current = self.posts_done

        # --- Здесь выполняется собственно работа ---
        self.log(f"Выполняю задачу… пост #{current}")
        # Имитация работы: time.sleep(...) или реальный вызов API.

        # Обновляем строку состояния в GUI-потоке.
        self.root.after(0, lambda: self.status_var.set(
            f"Работает — постов: {current}"
        ))

    # ------------------------------------------------------------------
    # Обработчики кнопок
    # ------------------------------------------------------------------
    def on_start(self):
        """Старт: создаём задачу с заданным интервалом."""
        interval = self._get_int(self.interval_var.get(), None)
        limit = self._get_int(self.limit_var.get(), None)

        # Валидация ввода.
        if interval is None or interval <= 0:
            messagebox.showerror("Ошибка", "Интервал должен быть положительным числом.")
            return
        if limit is None or limit <= 0:
            messagebox.showerror("Ошибка", "Лимит постов должен быть положительным числом.")
            return

        if self.job is None:
            # Сбрасываем счётчик при новом запуске.
            with self.posts_lock:
                self.posts_done = 0

            # Добавляем задачу в планировщик. APScheduler сам вызовет
            # do_work() в своём пуле потоков с заданным интервалом.
            self.job = self.scheduler.add_job(
                self.do_work,
                trigger=IntervalTrigger(seconds=interval),
                id=self.job_id,
                max_instances=1,         # не запускать новый экземпляр, пока идёт прошлый
                replace_existing=True,
            )
            self.log(f"Старт. Интервал: {interval} сек, лимит: {limit}.")
        else:
            # Если задача была на паузе — возобновляем.
            self.job.resume()
            self.log("Возобновлено.")

        self.status_var.set("Работает")
        self._set_buttons(start=False, pause=True, stop=True)

    def on_pause(self):
        """Пауза: приостанавливаем задачу, не удаляя её."""
        if self.job is not None:
            self.job.pause()
            self.log("Пауза.")
            self.status_var.set("Пауза")
            self._set_buttons(start=True, pause=False, stop=True)

    def on_stop(self):
        """Стоп: удаляем задачу и сбрасываем состояние."""
        if self.job is not None:
            self.scheduler.remove_job(self.job_id)
            self.job = None
            self.log("Остановлено.")
        self.status_var.set("Остановлено")
        self._set_buttons(start=True, pause=False, stop=False)

    def _set_buttons(self, start, pause, stop):
        """Удобный помощник для включения/выключения кнопок."""
        self.start_btn.configure(state="normal" if start else "disabled")
        self.pause_btn.configure(state="normal" if pause else "disabled")
        self.stop_btn.configure(state="normal" if stop else "disabled")

    # ------------------------------------------------------------------
    # Работа с config.json
    # ------------------------------------------------------------------
    def load_config(self):
        """Загрузить настройки из файла (или взять значения по умолчанию)."""
        config = dict(DEFAULT_CONFIG)
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    config.update(json.load(f))
            except (json.JSONDecodeError, OSError) as e:
                self.log(f"Не удалось прочитать config.json: {e}")
        # Заполняем поля ввода загруженными значениями.
        self.interval_var.set(str(config.get("interval", DEFAULT_CONFIG["interval"])))
        self.limit_var.set(str(config.get("limit", DEFAULT_CONFIG["limit"])))

    def save_config(self):
        """Сохранить текущие настройки в файл."""
        config = {
            "interval": self._get_int(self.interval_var.get(), DEFAULT_CONFIG["interval"]),
            "limit": self._get_int(self.limit_var.get(), DEFAULT_CONFIG["limit"]),
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.log(f"Не удалось сохранить config.json: {e}")

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------
    @staticmethod
    def _get_int(value, default):
        """Безопасное преобразование строки в int."""
        try:
            return int(str(value).strip())
        except (ValueError, TypeError):
            return default

    def on_close(self):
        """Корректное завершение: сохранить настройки и погасить планировщик."""
        self.save_config()
        if self.scheduler.running:
            # wait=False — не ждём завершения текущих задач при выходе.
            self.scheduler.shutdown(wait=False)
        self.root.destroy()


def main():
    root = tk.Tk()
    app = SchedulerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
