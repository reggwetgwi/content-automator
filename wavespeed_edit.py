# -*- coding: utf-8 -*-
"""
Обёртка над Wavespeed API для модели alibaba/wan-2.7/image-edit.

Wavespeed работает асинхронно:
  1. POST с задачей -> возвращается id и URL для опроса (poll).
  2. Опрашиваем результат, пока статус не станет "completed"
     (или "failed").
  3. Забираем URL готового изображения из outputs.

Ключ берётся из переменной окружения WAVESPEED_API_KEY (.env).
"""

import os
import time
import logging

import requests
from dotenv import load_dotenv

# Загружаем переменные окружения из .env в корне проекта.
load_dotenv()

logger = logging.getLogger(__name__)

# --- Константы конфигурации ---
API_KEY = os.getenv("WAVESPEED_API_KEY")
BASE_URL = "https://api.wavespeed.ai/api/v3"
MODEL = "alibaba/wan-2.7/image-edit"
SUBMIT_URL = f"{BASE_URL}/{MODEL}"

# Параметры повторных попыток и поллинга.
MAX_RETRIES = 3          # попытки для сетевых сбоев на этапе отправки
RETRY_BACKOFF = 2.0      # базовая задержка между попытками (экспоненциальная)
POLL_INTERVAL = 2.0      # пауза между опросами результата, сек
POLL_TIMEOUT = 300       # общий лимит ожидания результата, сек


class WavespeedError(Exception):
    """Единый тип ошибки для всех проблем при работе с API."""


def _require_api_key():
    """Проверяем, что ключ задан, иначе нет смысла идти дальше."""
    if not API_KEY:
        raise WavespeedError(
            "Не найден WAVESPEED_API_KEY. Добавьте его в файл .env."
        )


def _submit_task(image_url, prompt, session, extra_params=None):
    """Отправляет задачу и возвращает (request_id, poll_url).

    Делает несколько попыток при сетевых ошибках/5xx.
    """
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    # Тело запроса. Имена полей соответствуют схеме image-edit:
    # входное изображение + текстовый промпт.
    payload = {
        "image": image_url,
        "prompt": prompt,
    }
    if extra_params:
        payload.update(extra_params)

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.post(
                SUBMIT_URL, headers=headers, json=payload, timeout=30
            )
            # 4xx (кроме 429) — ошибка запроса, повторять бессмысленно.
            if resp.status_code in (400, 401, 403, 404, 422):
                raise WavespeedError(
                    f"Ошибка запроса {resp.status_code}: {resp.text}"
                )
            # 429 и 5xx — временные, имеет смысл повторить.
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.RequestException(
                    f"Временная ошибка {resp.status_code}: {resp.text}"
                )

            resp.raise_for_status()
            data = resp.json().get("data", {})

            request_id = data.get("id")
            # URL для опроса обычно лежит в urls.get; на всякий случай
            # собираем его сами, если поля нет.
            poll_url = (data.get("urls") or {}).get("get")
            if not poll_url and request_id:
                poll_url = f"{BASE_URL}/predictions/{request_id}/result"

            if not request_id or not poll_url:
                raise WavespeedError(f"Неожиданный ответ при отправке: {data}")

            logger.info("Задача отправлена, id=%s", request_id)
            return request_id, poll_url

        except (requests.RequestException, ValueError) as e:
            # ValueError ловит проблемы с разбором JSON.
            last_error = e
            if attempt < MAX_RETRIES:
                delay = RETRY_BACKOFF * (2 ** (attempt - 1))
                logger.warning(
                    "Попытка %d/%d не удалась: %s. Повтор через %.1f с",
                    attempt, MAX_RETRIES, e, delay,
                )
                time.sleep(delay)
            else:
                raise WavespeedError(
                    f"Не удалось отправить задачу после {MAX_RETRIES} попыток: {e}"
                ) from last_error


def _poll_result(poll_url, session):
    """Опрашивает статус задачи, пока не получит результат или ошибку."""
    headers = {"Authorization": f"Bearer {API_KEY}"}
    deadline = time.monotonic() + POLL_TIMEOUT

    while time.monotonic() < deadline:
        try:
            resp = session.get(poll_url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json().get("data", {})
        except (requests.RequestException, ValueError) as e:
            # Сбой при опросе не критичен — подождём и попробуем снова,
            # пока не вышел общий таймаут.
            logger.warning("Сбой при опросе: %s. Повтор...", e)
            time.sleep(POLL_INTERVAL)
            continue

        status = data.get("status")

        if status == "completed":
            outputs = data.get("outputs") or []
            if not outputs:
                raise WavespeedError("Задача завершена, но outputs пуст.")
            logger.info("Готово.")
            return outputs[0]  # URL результата

        if status == "failed":
            raise WavespeedError(f"Задача завершилась с ошибкой: {data.get('error')}")

        # Статусы created / processing / queued — ждём дальше.
        logger.debug("Статус: %s", status)
        time.sleep(POLL_INTERVAL)

    raise WavespeedError(f"Превышено время ожидания результата ({POLL_TIMEOUT} с).")


def edit_image(image_url, prompt, extra_params=None):
    """Главная функция.

    Аргументы:
        image_url    — URL исходного изображения.
        prompt       — текстовый промпт для редактирования.
        extra_params — необязательный dict с доп. параметрами модели
                       (например, seed, output_format и т. п.).

    Возвращает:
        str — URL результирующего изображения.

    Бросает:
        WavespeedError — при любой невосстановимой ошибке.
    """
    _require_api_key()

    if not image_url or not prompt:
        raise WavespeedError("image_url и prompt обязательны.")

    # Один сессионный объект на отправку + поллинг (переиспользование соединений).
    with requests.Session() as session:
        _, poll_url = _submit_task(image_url, prompt, session, extra_params)
        return _poll_result(poll_url, session)


if __name__ == "__main__":
    # Небольшой самотест при прямом запуске: python wavespeed_edit.py
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        result = edit_image(
            image_url="https://example.com/input.jpg",
            prompt="make the background a sunny beach",
        )
        print("Результат:", result)
    except WavespeedError as e:
        print("Ошибка:", e)
