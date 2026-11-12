import asyncio
import hmac
import os
from pathlib import Path
import textwrap
import time
import asyncssh
from idc_simulator.cli import CliSession


class AuthServer(asyncssh.SSHServer):
    def __init__(self, credentials, connections):
        self.credentials, self.connections = credentials, connections

    def connection_made(self, connection):
        self.connection = connection
        self.connections.add(connection)

    def connection_lost(self, exc):
        self.connections.discard(self.connection)

    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str):
            return False
        return (hmac.compare_digest(username.encode("utf-8"), str(self.credentials["ssh_user"]).encode("utf-8"))
                and hmac.compare_digest(password.encode("utf-8"), str(self.credentials["ssh_password"]).encode("utf-8")))


class SshServer:
    def __init__(self, client, address, port, key_path, credentials, metrics):
        self.client, self.address, self.port = client, address, port
        self.key_path = Path(key_path)
        self.credentials, self.metrics = credentials, metrics
        self.server = None
        self.tasks = set()
        self.connections = set()

    async def start(self):
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.key_path.exists():
            key = asyncssh.generate_private_key("ssh-ed25519")
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(key.export_private_key())
        self.server = await asyncssh.create_server(lambda: AuthServer(self.credentials, self.connections), self.address, self.port,
            server_host_keys=[str(self.key_path)], process_factory=self.process, line_editor=False,
            login_timeout=20)

    async def process(self, process):
        task = asyncio.current_task(); self.tasks.add(task)
        self.metrics.increment("ssh_sessions_total")
        session = CliSession(self.client)
        try:
            if process.command is not None:
                outputs = []
                for command in process.command.splitlines():
                    outputs.append(await session.execute(command))
                output = "\n".join(outputs)
                process.stdout.write(output+"\n")
                process.exit(1 if "Error:" in output else 0)
                return
            process.stdout.write(session.prompt)
            line = ""
            last_cr = False
            while not session.closed:
                try:
                    char = await process.stdin.read(1)
                except asyncssh.TerminalSizeChanged as exc:
                    session.width = max(20, exc.width); continue
                if not char:
                    break
                if char == "\x03":
                    line = ""; process.stdout.write("^C\r\n"+session.prompt); continue
                if char == "\x04":
                    break
                if char in "\x08\x7f":
                    if line:
                        line = line[:-1]; process.stdout.write("\b \b")
                    continue
                if char in "\r\n":
                    if char == "\n" and last_cr:
                        last_cr = False; continue
                    last_cr = char == "\r"
                    process.stdout.write("\r\n")
                    started = time.monotonic()
                    result = await session.execute(line)
                    self.metrics.observe("ssh_command_seconds", time.monotonic()-started)
                    await self.output(process, session, result)
                    line = ""
                    if not session.closed:
                        process.stdout.write(session.prompt)
                elif char.isprintable() and len(line) < 4096:
                    line += char; last_cr = False; process.stdout.write(char)
            process.exit(0)
        except (asyncssh.Error, ConnectionError):
            pass
        finally:
            self.tasks.discard(task)

    async def output(self, process, session, output):
        lines = [part for line in output.splitlines() for part in (textwrap.wrap(line, width=session.width, replace_whitespace=False) or [""])]
        height = process.term_size[1] or session.page_length
        page = min(height, session.page_length) if session.page_length else 0
        for n, line in enumerate(lines, 1):
            process.stdout.write(line+"\r\n")
            if page and n % max(1, page-1) == 0 and n < len(lines):
                process.stdout.write("--More--")
                try:
                    char = await asyncio.wait_for(process.stdin.read(1), 120)
                except asyncio.TimeoutError:
                    process.stdout.write("\r        \r")
                    break
                process.stdout.write("\r        \r")
                if char in {"q", "\x03", ""}:
                    break

    async def close(self):
        if self.server:
            self.server.close(); await self.server.wait_closed()
        connections = tuple(self.connections)
        for connection in connections:
            connection.close()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        await asyncio.gather(*(connection.wait_closed() for connection in connections), return_exceptions=True)
