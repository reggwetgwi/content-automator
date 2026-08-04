# -*- coding: utf-8 -*-
"""
test_editor.py — быстрый автономный тест ImageEditor без GUI.

Прогоняет один вызов merge(source, target, prompt) и печатает результат.
Нужен, чтобы проверить:
  * читается ли ключ из .env;
  * угаданы ли имена полей API (images / prompt / outputs ...);
  * доходит ли задача до статуса completed.

Запуск:
    python test_editor.py
Можно переопределить промпт/картинки переменными окружения:
    TEST_PROMPT, TEST_SOURCE, TEST_TARGET
"""

import os
import sys
import logging

from image_editor import ImageEditor, ImageEditorError

# Подробный лог в консоль, чтобы видеть отправку и поллинг.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# Тестовые входные изображения (публичные плейсхолдеры).
# Замени на свои URL или локальные пути при необходимости.
SOURCE = os.getenv("TEST_SOURCE", "https://picsum.photos/id/64/800/800")
TARGET = os.getenv("TEST_TARGET", "https://picsum.photos/id/65/800/800")
PROMPT = os.getenv("TEST_PROMPT", "blend the two images into one cohesive scene")


def main():
    print(f"source: {SOURCE}")
    print(f"target: {TARGET}")
    print(f"prompt: {PROMPT}")
    print("-" * 50)

    try:
        editor = ImageEditor()
    except ImageEditorError as e:
        # Чаще всего сюда падают из-за пустого WAVESPEED_API_KEY в .env.
        print(f"Инициализация не удалась: {e}")
        sys.exit(1)

    try:
        result_url = editor.merge(SOURCE, TARGET, PROMPT)
    except ImageEditorError as e:
        print(f"Обработка не удалась: {e}")
        print("\nЕсли ошибка про неверные поля/422 — пришли пример запроса")
        print("из доков модели, поправим payload в image_editor.py.")
        sys.exit(1)

    print("-" * 50)
    print(f"ГОТОВО. URL результата:\n{result_url}")


if __name__ == "__main__":
    main()
