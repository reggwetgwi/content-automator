# -*- coding: utf-8 -*-
"""
image_editor.py — класс ImageEditor для main.py.

Оборачивает Wavespeed API (alibaba/wan-2.7/image-edit) в метод
merge(source, target, prompt): отправляет два изображения и текстовый
промпт, ждёт результат, возвращает URL готового изображения.

Промпт приходит снаружи (из поля GUI), внутри не зашит.
Ключ — из .env (WAVESPEED_API_KEY).

Логика запроса/повторов/поллинга повторяет wavespeed_edit.py.
"""

import os
import time
import base64
import mimetypes
import logging

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("pipeline")

# --- Конфигурация ---
API_KEY = os.getenv("WAVESPEED_API_KEY")
BASE_URL = "https://api.wavespeed.ai/api/v3"
MODEL = "alibaba/wan-2.7/image-edit"
SUBMIT_URL = f"{BASE_URL}/{MODEL}"

MAX_RETRIES = 3        # попытки при сетевых сбоях на отправке
RETRY_BACKOFF = 2.0    # базовая задержка, экспоненциальный рост
POLL_INTERVAL = 2.0    # пауза между опросами результата, сек
POLL_TIMEOUT = 300     # общий лимит ожидания, сек


class ImageEditorError(Exception):
    """Единый тип ошибки обработки."""


class ImageEditor:
    """Обёртка над Wavespeed image-edit с повторными попытками."""

    def __init__(self):
        if not API_KEY:
            raise ImageEditorError("Не найден WAVESPEED_API_KEY в .env.")
        self.session = requests.Session()

    # ------------------------------------------------------------------
    @staticmethod
    def _to_image_ref(path_or_url):
        """Локальный путь -> data-URI (base64); URL -> как есть.

        Wavespeed принимает изображение либо как URL, либо как
        base64 data-URI. Локальные файлы кодируем в data-URI.
        """
        if str(path_or_url).startswith(("http://", "https://")):
            return path_or_url
        # Локальный файл -> base64 data-URI (незадокументированный путь).
        logger.warning("Локальный файл '%s' кодируется в base64 — API официально "
                       "ждёт URL. Если получишь 422, залей картинку на хостинг "
                       "и подай ссылку.", path_or_url)
        mime = mimetypes.guess_type(path_or_url)[0] or "image/jpeg"
        with open(path_or_url, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return f"data:{mime};base64,{b64}"

    # ------------------------------------------------------------------
    def merge(self, reference, source, prompt, size=None, seed=None):
        """Объединить изображения по промпту. Вернуть URL результата.

        ПОРЯДОК ВАЖЕН: reference идёт в images[0], source — в images[1].
        reference — твоё референсное изображение (из ./references/).
        source    — эталон/исходник (из ./source_images/).
        prompt    — текст инструкции (из GUI), обязателен.
        size      — опц. строка "ШxВ", напр. "1024*1024".
        seed      — опц. int; -1 = случайный.

        ВНИМАНИЕ: по докам Wavespeed поле images принимает URL.
        Локальные файлы кодируются в base64 data-URI как запасной вариант,
        но он официально не документирован — надёжнее подавать публичные URL.
        """
        if not prompt:
            raise ImageEditorError("Пустой промпт.")

        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
        # images — массив из 1–9 ссылок. Референс первым, source вторым.
        payload = {
            "images": [self._to_image_ref(reference), self._to_image_ref(source)],
            "prompt": prompt,
        }
        # Необязательные параметры добавляем только если заданы.
        if size:
            payload["size"] = size
        if seed is not None:
            payload["seed"] = seed

        request_id, poll_url = self._submit(headers, payload)
        return self._poll(poll_url)

    # ------------------------------------------------------------------
    def _submit(self, headers, payload):
        """Отправить задачу с повторами при временных сбоях."""
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.post(
                    SUBMIT_URL, headers=headers, json=payload, timeout=30
                )
                # 4xx (кроме 429) — повторять бессмысленно.
                if resp.status_code in (400, 401, 403, 404, 422):
                    raise ImageEditorError(
                        f"Ошибка запроса {resp.status_code}: {resp.text}"
                    )
                # 429 / 5xx — временные.
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.RequestException(
                        f"Временная ошибка {resp.status_code}"
                    )
                resp.raise_for_status()

                data = resp.json().get("data", {})
                request_id = data.get("id")
                poll_url = (data.get("urls") or {}).get("get")
                if not poll_url and request_id:
                    poll_url = f"{BASE_URL}/predictions/{request_id}/result"
                if not request_id or not poll_url:
                    raise ImageEditorError(f"Неожиданный ответ: {data}")

                logger.info("Wavespeed: задача отправлена, id=%s", request_id)
                return request_id, poll_url

            except (requests.RequestException, ValueError) as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    delay = RETRY_BACKOFF * (2 ** (attempt - 1))
                    logger.warning("Попытка %d/%d не удалась: %s. Повтор через %.1fс",
                                   attempt, MAX_RETRIES, e, delay)
                    time.sleep(delay)
                else:
                    raise ImageEditorError(
                        f"Не удалось отправить после {MAX_RETRIES} попыток: {e}"
                    ) from last_error

    # ------------------------------------------------------------------
    def _poll(self, poll_url):
        """Опрашивать статус до completed/failed или таймаута."""
        headers = {"Authorization": f"Bearer {API_KEY}"}
        deadline = time.monotonic() + POLL_TIMEOUT

        while time.monotonic() < deadline:
            try:
                resp = self.session.get(poll_url, headers=headers, timeout=30)
                resp.raise_for_status()
                data = resp.json().get("data", {})
            except (requests.RequestException, ValueError) as e:
                logger.warning("Сбой при опросе: %s. Повтор...", e)
                time.sleep(POLL_INTERVAL)
                continue

            status = data.get("status")
            if status == "completed":
                outputs = data.get("outputs") or []
                if not outputs:
                    raise ImageEditorError("Готово, но outputs пуст.")
                return outputs[0]
            if status == "failed":
                raise ImageEditorError(f"Задача с ошибкой: {data.get('error')}")
            time.sleep(POLL_INTERVAL)

        raise ImageEditorError(f"Таймаут ожидания результата ({POLL_TIMEOUT}с).")
