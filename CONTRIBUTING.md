# Разработка

Форк исходной интеграции iljyxa, адаптированный под авторизацию подтверждающим звонком.
Используйте Python 3.14 и отдельное виртуальное окружение:

```sh
python3.14 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
ruff check custom_components tests
pytest tests/ -q
```

Для тестов камер нужен системный ffmpeg и libturbojpeg. Не запускайте второй HA на рабочей малинке.

Локальная демонстрация страницы с тестовым сервисом:

```sh
python tools/preview_callback.py
```

Откройте http://127.0.0.1:18088/api/rosdomofon/callback/demo-pending.
Доступны состояния demo-expired, demo-confirmed и demo-error. Звонить на демонстрационные номера не нужно.
Тесты HTTP выполняют настоящий HA config flow с подменёнными ответами API. Они не проверяют реальный звонок с панели — такая проверка нужна отдельно.

Не прикладывайте access/refresh-токены, push-реквизиты, номера телефонов и содержимое .storage или .rosdomofon к issues и коммитам.
