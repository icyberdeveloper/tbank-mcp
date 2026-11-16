# Запуск на VPS через OpenAI Secure MCP Tunnel

На VPS ничего собирать не нужно: Compose скачивает готовый образ с Chromium.
Сервер работает через stdio и `openai/tunnel-client`, без открытых портов.

## 1. Один раз подготовьте доступ

Нужна Linux VPS с установленным **Docker и Compose v2**. Можно выбрать готовый
Docker-образ ОС в панели хостинга или установить Docker по
[официальной инструкции](https://docs.docker.com/engine/install/).
Команды ниже запускайте от пользователя с доступом к Docker.
Готовый образ предназначен для Linux amd64 (обычные Intel/AMD VPS).

В [OpenAI Platform → Tunnels](https://platform.openai.com/settings/organization/tunnels)
создайте туннель и скопируйте его **Tunnel ID**. Создайте
[runtime API key](https://platform.openai.com/settings/organization/api-keys)
с правами Tunnels Read + Use. Нужен доступ к Secure MCP Tunnel в вашем аккаунте
и рабочем пространстве ChatGPT.

Скачайте только два небольших файла:

```bash
mkdir -p ~/tbank && cd ~/tbank
curl -fsSLO https://raw.githubusercontent.com/icyberdeveloper/tbank-mcp/master/compose.yaml
curl -fsSLO https://raw.githubusercontent.com/icyberdeveloper/tbank-mcp/master/tbank
```

## 2. Настройте и запустите одной командой

```bash
bash tbank setup
```

Скрипт сам:

1. Спросит Tunnel ID и ключ OpenAI; ключ вводится скрыто.
2. Скачает готовый Docker-образ.
3. Спросит телефон Т-Банка, затем запустит обычный вход по SMS и паролю/PIN.
4. Сохранит банковскую сессию и запустит контейнер в фоне.

Пароль и SMS вводятся только в вашем терминале, без ChatGPT.
Ключ OpenAI сохраняется в локальном `.env` с правами 0600; файл не публикуется.
Банковская сессия сохраняется в Docker volume `tbank-mcp-data` и переживает
обновления контейнера. Один контейнер обслуживает один банковский аккаунт.

## 3. Подключите ChatGPT

Дождитесь статуса `healthy`:

```bash
bash tbank status
```

Выберите созданный туннель в
[настройках приложений ChatGPT](https://chatgpt.com/#settings/Connectors).
Если интерфейс вашего workspace отличается, следуйте
[инструкции OpenAI](https://github.com/openai/tunnel-client/blob/v0.0.21/docs/onboarding.md).
Контейнер должен оставаться запущенным. Сначала проверьте статус банковской сессии
и чтение данных. Платежи требуют поддержки подтверждений MCP elicitation клиентом;
проверки подтверждений остаются включёнными.

## На каждый день

```bash
docker compose up -d  # запустить; готовый образ скачается автоматически
bash tbank login     # повторный вход в банк: скрипт сам остановит и запустит сервер
bash tbank update    # скачать свежий образ и перезапустить
bash tbank logs      # посмотреть последние сообщения
bash tbank stop      # остановить
```

Запускайте команды из `~/tbank`. Не запускайте несколько экземпляров для одной
сессии и Tunnel ID. `docker compose down` сохраняет сессию; удаление volume
`tbank-mcp-data` удаляет её. Перед резервной копией volume остановите контейнер.
Если вызов платежа прервался, сначала проверьте его статус, не повторяйте вслепую.

## Для сопровождающего репозитория

Workflow `Docker` проверяет тесты, собирает образ, запускает Chromium и проверяет
stdio MCP. После успешного push в `master` он публикует проверенный образ:

- `ghcr.io/icyberdeveloper/tbank-mcp:tunnel` — используется Compose;
- `ghcr.io/icyberdeveloper/tbank-mcp:sha-<commit>` — конкретная версия.

Первый образ появится **после слияния этого PR и успешного workflow на master**.
После первой публикации откройте пакет в GitHub → Packages → tbank-mcp → Package
settings и один раз установите visibility **Public**, чтобы VPS скачивала образ
без `docker login`. Это настройка пакета, а не действие на каждой VPS.
До этого момента инструкция с `master` и готовым образом ещё не работает.

Сборка для разработки остаётся доступна:

```bash
docker build --target tunnel -t tbank-mcp:tunnel .
pip install .
python tests/docker_smoke.py
```

Туннель использует OpenAI runtime `v0.0.21` без Cloudflare companion. Live-проверка
ChatGPT и банка требует ваших Tunnel ID, ключа и входа; CI их не использует.
