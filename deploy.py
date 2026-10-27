"""Деплой бота на сервер по SSH: ставит Docker, заливает код, пишет .env и запускает.

Запуск: deploy.bat (или py deploy.py). Повторный запуск обновляет бота, база и слежки сохраняются.
Пароль сервера нигде не сохраняется; в deploy.json запоминаются только IP, порт и пользователь.
"""

import getpass
import json
import shlex
import sys
import time
from pathlib import Path

try:
    import paramiko
except ImportError:
    sys.exit("Нет библиотеки paramiko: py -m pip install paramiko")

ROOT = Path(__file__).resolve().parent
REMOTE_DIR = "/opt/emoji-bot"
UPLOAD_FILES = ["bot.py", "requirements.txt", "Dockerfile", "docker-compose.yml", ".dockerignore"]
UPLOAD_DIRS = ["app"]
SKIP_PARTS = {"__pycache__", ".pytest_cache"}
SETTINGS = ROOT / "deploy.json"


def ask(prompt: str, default: str | None = None, secret: bool = False) -> str:
    hint = f" [{default}]" if default and not secret else (" [Enter: как сейчас]" if default else "")
    while True:
        value = (getpass.getpass if secret else input)(f"{prompt}{hint}: ").strip()
        if value:
            return value
        if default:
            return default
        print("  Нужно заполнить.")


def parse_env(text: str) -> dict[str, str]:
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def render_env(old_text: str, updates: dict[str, str]) -> str:
    """Обновляет значения в существующем .env, сохраняя комментарии и остальные строки."""
    lines, seen = [], set()
    for line in old_text.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else None
        if key in updates:
            lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            lines.append(line)
    lines += [f"{key}={value}" for key, value in updates.items() if key not in seen]
    return "\n".join(lines).strip() + "\n"


class Server:
    def __init__(self, host: str, port: int, user: str, password: str):
        self.user, self.password = user, password
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            host, port=port, username=user, password=password, timeout=20, allow_agent=False, look_for_keys=False
        )
        key = self.client.get_transport().get_remote_server_key()
        print(f"  Ключ сервера: {key.get_name()} {key.get_fingerprint().hex(':')}")

    def run(self, command: str, check: bool = True, quiet: bool = False) -> int:
        """Выполняет команду (через sudo, если вошли не под root) и печатает вывод по ходу."""
        if self.user != "root":
            command = f"sudo -S -p '' bash -c {shlex.quote(command)}"
        stdin, stdout, _ = self.client.exec_command(command, get_pty=True)
        if self.user != "root":
            stdin.write(self.password + "\n")
            stdin.flush()
        for line in iter(stdout.readline, ""):
            if not quiet:
                print("  │ " + line.rstrip())
        code = stdout.channel.recv_exit_status()
        if check and code != 0:
            raise RuntimeError(f"Команда завершилась с кодом {code}: {command[:80]}")
        return code

    def upload(self, local_root: Path, remote_root: str) -> int:
        sftp = self.client.open_sftp()
        count = 0

        def mkdir(path: str) -> None:
            try:
                sftp.stat(path)
            except FileNotFoundError:
                sftp.mkdir(path)

        for name in UPLOAD_FILES:
            sftp.put(str(local_root / name), f"{remote_root}/{name}")
            count += 1
        for folder in UPLOAD_DIRS:
            for path in sorted((local_root / folder).rglob("*")):
                if SKIP_PARTS & set(path.parts):
                    continue
                remote = f"{remote_root}/{path.relative_to(local_root).as_posix()}"
                if path.is_dir():
                    mkdir(remote)
                else:
                    mkdir(remote.rsplit("/", 1)[0])
                    sftp.put(str(path), remote)
                    count += 1
        sftp.close()
        return count

    def read_file(self, path: str) -> str:
        sftp = self.client.open_sftp()
        try:
            with sftp.open(path) as f:
                return f.read().decode("utf-8", "replace")
        except FileNotFoundError:
            return ""
        finally:
            sftp.close()

    def write_file(self, path: str, text: str, mode: int = 0o600) -> None:
        sftp = self.client.open_sftp()
        with sftp.open(path, "w") as f:
            f.write(text)
        sftp.chmod(path, mode)
        sftp.close()


def step(title: str) -> None:
    print(f"\n▶ {title}")


def main() -> None:
    print("=== Деплой Emoji Studio на сервер ===\n")
    saved = json.loads(SETTINGS.read_text("utf-8")) if SETTINGS.exists() else {}
    local_env = parse_env((ROOT / ".env").read_text("utf-8")) if (ROOT / ".env").exists() else {}

    host = ask("IP или домен сервера", saved.get("host"))
    port = int(ask("SSH порт", str(saved.get("port", 22))))
    user = ask("Пользователь", saved.get("user", "root"))
    password = ask("Пароль сервера", secret=True)
    token = ask("BOT_TOKEN от @BotFather", local_env.get("BOT_TOKEN"), secret=True)
    admins = ask("ADMIN_IDS (твой ID, через запятую)", local_env.get("ADMIN_IDS") or saved.get("admins"))

    step(f"Подключаюсь к {user}@{host}:{port}")
    try:
        server = Server(host, port, user, password)
    except paramiko.AuthenticationException:
        sys.exit("✖ Неверный логин или пароль.")
    except Exception as e:
        sys.exit(f"✖ Не удалось подключиться: {e}")
    SETTINGS.write_text(json.dumps({"host": host, "port": port, "user": user, "admins": admins}, indent=2), "utf-8")

    step("Проверяю Docker")
    if server.run("command -v docker", check=False, quiet=True) != 0:
        print("  Docker не найден, устанавливаю (1–3 минуты)…")
        server.run("curl -fsSL https://get.docker.com | sh")
    if server.run("docker compose version", check=False, quiet=True) != 0:
        server.run("apt-get update -qq && apt-get install -y -qq docker-compose-plugin")
    server.run("systemctl enable --now docker", check=False, quiet=True)
    print("  ✔ Docker готов")

    step(f"Создаю папку {REMOTE_DIR} и заливаю файлы")
    server.run(f"mkdir -p {REMOTE_DIR} && chown -R {shlex.quote(user)} {REMOTE_DIR}", quiet=True)
    print(f"  ✔ Загружено файлов: {server.upload(ROOT, REMOTE_DIR)}")

    step("Пишу настройки .env")
    old_env = server.read_file(f"{REMOTE_DIR}/.env") or (ROOT / ".env.example").read_text("utf-8")
    server.write_file(f"{REMOTE_DIR}/.env", render_env(old_env, {"BOT_TOKEN": token, "ADMIN_IDS": admins}))
    print("  ✔ .env сохранён (доступ только владельцу)")

    step("Собираю и запускаю бота (первый раз 2–5 минут)")
    server.run(f"cd {REMOTE_DIR} && docker compose up -d --build --remove-orphans")

    step("Проверяю запуск")
    time.sleep(8)
    server.run(f"cd {REMOTE_DIR} && docker compose ps && docker compose logs --tail 15", check=False)

    print("\n✔ Готово! Бот работает на сервере и перезапускается сам.")
    print("  ВАЖНО: останови бота на компьютере, иначе две копии будут мешать друг другу.")
    print(f"  Логи на сервере: cd {REMOTE_DIR} && docker compose logs -f")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОтменено.")
    except RuntimeError as e:
        print(f"\n✖ Ошибка: {e}")
