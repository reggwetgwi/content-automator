# -*- coding: utf-8 -*-
"""
Класс для автопостинга фото в Instagram через instagrapi.

Особенности:
  * Логин/пароль берутся из .env.
  * Сессия сохраняется в файл (session.json) и переиспользуется,
    чтобы не логиниться заново каждый раз (меньше риск блокировок).
  * Метод publish(image_path, caption) публикует одиночное фото.
  * Обработка ошибок с логированием.
  * 2FA не обходится: если аккаунт требует код, это просто логируется
    и пробрасывается понятная ошибка.
"""

import os
import logging
from pathlib import Path

from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired,
    TwoFactorRequired,
    ChallengeRequired,
    ClientError,
    PleaseWaitFewMinutes,
)

load_dotenv()

logger = logging.getLogger(__name__)


class InstagramPosterError(Exception):
    """Единый тип ошибки для проблем публикации."""


class InstagramPoster:
    """Тонкая обёртка над instagrapi.Client с сохранением сессии."""

    def __init__(self, session_file="session.json"):
        # Учётные данные из окружения.
        self.username = os.getenv("IG_USERNAME")
        self.password = os.getenv("IG_PASSWORD")
        if not self.username or not self.password:
            raise InstagramPosterError(
                "Не заданы IG_USERNAME / IG_PASSWORD в .env."
            )

        self.session_file = Path(session_file)
        self.client = Client()
        # Небольшая задержка между запросами — снижает риск рейт-лимитов.
        self.client.delay_range = [1, 3]
        self._logged_in = False

    # ------------------------------------------------------------------
    # Авторизация
    # ------------------------------------------------------------------
    def login(self):
        """Логин с переиспользованием сохранённой сессии.

        Логика:
          1. Если есть session.json — загружаем настройки сессии.
          2. Пробуем логин: instagrapi подставит cookies из сессии.
          3. Проверяем сессию лёгким запросом. Если протухла —
             логинимся заново уже по паролю.
          4. Сохраняем актуальную сессию в файл.
        """
        if self._logged_in:
            return

        try:
            # --- Попытка через сохранённую сессию ---
            if self.session_file.exists():
                logger.info("Загружаю сохранённую сессию из %s", self.session_file)
                self.client.load_settings(self.session_file)
                self.client.login(self.username, self.password)
                try:
                    # Лёгкая проверка валидности сессии.
                    self.client.get_timeline_feed()
                except LoginRequired:
                    logger.warning("Сессия протухла — выполняю свежий логин.")
                    self._fresh_login()
            else:
                # --- Сессии нет — обычный логин ---
                self._fresh_login()

            self._logged_in = True
            self._save_session()
            logger.info("Успешный вход как %s", self.username)

        except TwoFactorRequired as e:
            # Намеренно НЕ обходим 2FA — только логируем.
            logger.error(
                "Аккаунт требует 2FA. Автопостинг невозможен без отключения "
                "двухфакторной аутентификации или ручного ввода кода."
            )
            raise InstagramPosterError("Требуется 2FA — вход прерван.") from e

        except ChallengeRequired as e:
            # Instagram запросил подтверждение (подозрительный вход).
            logger.error(
                "Instagram запросил challenge (подтверждение входа). "
                "Подтвердите вход вручную в приложении и повторите."
            )
            raise InstagramPosterError("Требуется подтверждение входа (challenge).") from e

        except PleaseWaitFewMinutes as e:
            logger.error("Instagram просит подождать (рейт-лимит): %s", e)
            raise InstagramPosterError("Рейт-лимит при входе. Повторите позже.") from e

        except ClientError as e:
            logger.exception("Ошибка клиента при входе.")
            raise InstagramPosterError(f"Не удалось войти: {e}") from e

    def _fresh_login(self):
        """Чистый логин по логину/паролю (сбрасывает прежние настройки)."""
        # Сбрасываем настройки, но сохраняем device/uuid, если они были,
        # чтобы Instagram видел «то же устройство».
        old_settings = self.client.get_settings()
        self.client.set_settings({})
        if old_settings.get("uuids"):
            self.client.set_uuids(old_settings["uuids"])
        self.client.login(self.username, self.password)

    def _save_session(self):
        """Сохраняем текущие настройки сессии в файл."""
        try:
            self.client.dump_settings(self.session_file)
            logger.debug("Сессия сохранена в %s", self.session_file)
        except OSError as e:
            logger.warning("Не удалось сохранить сессию: %s", e)

    # ------------------------------------------------------------------
    # Публикация
    # ------------------------------------------------------------------
    def publish(self, image_path, caption=""):
        """Публикует одиночное фото.

        Аргументы:
            image_path — путь к файлу изображения (jpg/png).
            caption    — подпись к посту.

        Возвращает:
            str — pk (идентификатор) опубликованного медиа.

        Бросает:
            InstagramPosterError — при любой ошибке публикации.
        """
        path = Path(image_path)
        if not path.exists():
            raise InstagramPosterError(f"Файл не найден: {path}")

        # Гарантируем, что вошли в аккаунт.
        if not self._logged_in:
            self.login()

        try:
            logger.info("Публикую %s ...", path.name)
            media = self.client.photo_upload(path, caption=caption)
            logger.info("Опубликовано. media pk=%s, code=%s", media.pk, media.code)
            return media.pk

        except LoginRequired as e:
            # Сессия отвалилась прямо во время работы — пробуем перелогиниться один раз.
            logger.warning("Требуется повторный вход. Пробую снова...")
            self._logged_in = False
            self.login()
            try:
                media = self.client.photo_upload(path, caption=caption)
                logger.info("Опубликовано после релогина. media pk=%s", media.pk)
                return media.pk
            except ClientError as e2:
                logger.exception("Повторная публикация не удалась.")
                raise InstagramPosterError(f"Не удалось опубликовать: {e2}") from e2

        except PleaseWaitFewMinutes as e:
            logger.error("Рейт-лимит при публикации: %s", e)
            raise InstagramPosterError("Рейт-лимит. Повторите позже.") from e

        except ClientError as e:
            logger.exception("Ошибка при публикации.")
            raise InstagramPosterError(f"Не удалось опубликовать: {e}") from e


if __name__ == "__main__":
    # Пример запуска: python instagram_poster.py
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        poster = InstagramPoster()
        poster.login()
        # poster.publish("photo.jpg", caption="Привет из автопостера #test")
    except InstagramPosterError as e:
        print("Ошибка:", e)
