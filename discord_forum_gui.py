#!/usr/bin/env python3
"""Small Tkinter launcher for the forum collector; Tkinter ships with Python."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


SCRIPT_DIR = Path(__file__).resolve().parent
COLLECTOR = SCRIPT_DIR / "discord_forum_collect.py"
DEFAULT_GUILD_ID = "1514607659758653560"
DEFAULT_CHANNEL_ID = "1515423283606982776"


class ForumCollectorGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Discord Forum Exporter")
        self.root.geometry("800x650")
        self.root.minsize(680, 480)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None

        self.guild_var = tk.StringVar(value=DEFAULT_GUILD_ID)
        self.channel_var = tk.StringVar(value=DEFAULT_CHANNEL_ID)
        self.post_id_var = tk.StringVar(value="1569385787621900359")
        self.output_var = tk.StringVar(value=str(SCRIPT_DIR / "discord_forum_export"))
        self.timeout_var = tk.StringVar(value="600")
        self.workers_var = tk.StringVar(value="4")
        self.download_var = tk.BooleanVar(value=True)
        self.voters_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Готов к запуску")

        self._build_ui()
        self.root.after(100, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 5}
        form = ttk.Frame(self.root, padding=10)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="ID сервера:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(form, textvariable=self.guild_var).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Label(form, text="ID форум-канала:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(form, textvariable=self.channel_var).grid(row=1, column=1, sticky="ew", **pad)

        ttk.Label(form, text="ID публикации для точечного теста (пусто = весь канал):").grid(
            row=2, column=0, sticky="w", **pad
        )
        ttk.Entry(form, textvariable=self.post_id_var).grid(row=2, column=1, sticky="ew", **pad)

        ttk.Label(form, text="Папка экспорта:").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(form, textvariable=self.output_var).grid(row=3, column=1, sticky="ew", **pad)
        ttk.Button(form, text="Обзор…", command=self._choose_output).grid(row=3, column=2, **pad)

        ttk.Label(form, text="Ожидать вход и загрузку (сек.):").grid(row=4, column=0, sticky="w", **pad)
        ttk.Spinbox(form, from_=60, to=3600, increment=60, textvariable=self.timeout_var, width=10).grid(
            row=4, column=1, sticky="w", **pad
        )
        ttk.Checkbutton(form, text="Скачивать вложенные файлы", variable=self.download_var).grid(
            row=5, column=1, sticky="w", **pad
        )
        ttk.Label(form, text="Параллельных задач (1–8):").grid(row=6, column=0, sticky="w", **pad)
        ttk.Spinbox(form, from_=1, to=8, textvariable=self.workers_var, width=10).grid(
            row=6, column=1, sticky="w", **pad
        )
        ttk.Checkbutton(form, text="Открывать ссылки со списком голосовавших и просмотр опроса", variable=self.voters_var).grid(
            row=7, column=1, sticky="w", **pad
        )

        note = (
            "Скрипт откроет Chromium; войдите в Discord в этом окне, если потребуется. "
            "«Полный сбор» обрабатывает публикации параллельно; «Тест публикации» — только ID выше. "
            "Скрипт может открыть безопасный просмотр опроса и сохраняет имена/роли только когда Discord показывает их; скрытые Discord сведения недоступны, варианты ответа не выбираются."
        )
        ttk.Label(form, text=note, wraplength=740, justify="left").grid(
            row=8, column=0, columnspan=3, sticky="ew", padx=8, pady=(8, 10)
        )

        controls = ttk.Frame(self.root, padding=(10, 0, 10, 5))
        controls.pack(fill="x")
        self.start_button = ttk.Button(
            controls, text="Полный сбор", command=lambda: self._start(test_mode=False)
        )
        self.start_button.pack(side="left", padx=5)
        self.test_button = ttk.Button(
            controls, text="Тест публикации", command=lambda: self._start(test_mode=True)
        )
        self.test_button.pack(side="left", padx=5)
        self.stop_button = ttk.Button(controls, text="Остановить", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=5)
        ttk.Button(controls, text="Открыть папку экспорта", command=self._open_output).pack(side="left", padx=5)
        ttk.Button(controls, text="Копировать лог", command=self._copy_log).pack(side="left", padx=5)
        self.progress = ttk.Progressbar(controls, mode="indeterminate")
        self.progress.pack(side="right", fill="x", expand=True, padx=8)

        ttk.Label(self.root, textvariable=self.status_var, anchor="w", padding=(12, 3)).pack(fill="x")
        log_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=16, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.log_menu = tk.Menu(self.root, tearoff=False)
        self.log_menu.add_command(label="Копировать выделенное", command=self._copy_log_selection)
        self.log_menu.add_command(label="Копировать весь лог", command=self._copy_log)
        self.log_menu.add_command(label="Выделить всё", command=self._select_all_log)
        self.log.bind("<Control-c>", self._copy_log_selection)
        self.log.bind("<Control-a>", self._select_all_log)
        self.log.bind("<Button-3>", self._show_log_menu)
        self._append_log("Выберите «Полный сбор» или «Тест публикации». Первый запуск может установить Playwright и Chromium.")

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _copy_text_to_clipboard(self, text: str) -> None:
        if not text:
            self.status_var.set("Нет текста для копирования")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self.status_var.set("Текст скопирован в буфер обмена")

    def _copy_log_selection(self, _event: tk.Event | None = None) -> str:
        try:
            selected = self.log.get("sel.first", "sel.last")
        except tk.TclError:
            return "break"
        self._copy_text_to_clipboard(selected)
        return "break"

    def _copy_log(self) -> None:
        try:
            text = self.log.get("sel.first", "sel.last")
        except tk.TclError:
            text = self.log.get("1.0", "end-1c")
        self._copy_text_to_clipboard(text)

    def _select_all_log(self, _event: tk.Event | None = None) -> str:
        self.log.tag_add("sel", "1.0", "end-1c")
        self.log.mark_set("insert", "end-1c")
        self.log.see("insert")
        return "break"

    def _show_log_menu(self, event: tk.Event) -> str:
        try:
            self.log_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.log_menu.grab_release()
        return "break"

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_var.get() or str(SCRIPT_DIR))
        if selected:
            self.output_var.set(selected)

    def _start(self, test_mode: bool = False) -> None:
        if self.process and self.process.poll() is None:
            return
        guild_id = self.guild_var.get().strip()
        channel_id = self.channel_var.get().strip()
        post_id = self.post_id_var.get().strip()
        if not guild_id.isdigit() or not channel_id.isdigit():
            messagebox.showerror("Неверные ID", "Введите числовые ID сервера и форум-канала.")
            return
        if test_mode and not post_id:
            messagebox.showerror("Нет ID публикации", "Введите ID публикации для точечного теста.")
            return
        if test_mode and (not post_id.isdigit() or not 16 <= len(post_id) <= 22):
            messagebox.showerror("Неверный ID публикации", "ID публикации должен содержать 16–22 цифры.")
            return
        try:
            timeout = int(self.timeout_var.get())
            workers = int(self.workers_var.get())
            if timeout < 1 or not 1 <= workers <= 8:
                raise ValueError
        except ValueError:
            messagebox.showerror("Неверные параметры", "Таймаут должен быть положительным, workers — от 1 до 8.")
            return

        output = Path(self.output_var.get()).expanduser()
        if not output.is_absolute():
            output = SCRIPT_DIR / output
        command = [
            sys.executable, "-u", str(COLLECTOR), guild_id, channel_id,
            "--output", str(output),
            "--profile", str(SCRIPT_DIR / ".discord_browser_profile"),
            "--wait-timeout", str(timeout),
            "--workers", str(workers),
        ]
        if test_mode:
            command.extend(["--post-id", post_id])
        if not self.download_var.get():
            command.append("--no-download-files")
        if not self.voters_var.get():
            command.append("--no-inspect-voters")

        mode = f"тест публикации {post_id}" if test_mode else f"полный сбор, workers={workers}"
        self._append_log(f"Запуск ({mode}): " + " ".join(command))
        self.status_var.set(f"Запускаю: {mode}…")
        self.start_button.configure(state="disabled")
        self.test_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.start(12)
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(SCRIPT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:
            self._finish(None)
            messagebox.showerror("Не удалось запустить", str(exc))
            return

        threading.Thread(target=self._read_output, args=(self.process,), daemon=True).start()

    def _read_output(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            self.events.put(("line", line.rstrip("\r\n")))
        self.events.put(("exit", process.wait()))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "line":
                    line = str(value)
                    self._append_log(line)
                    if "Собираю публикацию" in line or "Открываю публикацию" in line:
                        self.status_var.set(line)
                    elif "ждёт появления публикаций" in line:
                        self.status_var.set("Ожидаю входа и появления публикаций в открытом Chromium…")
                    elif "Готово:" in line:
                        self.status_var.set("Сбор завершён")
                elif kind == "exit":
                    code = int(value)
                    self._finish(code)
                    if code == 0:
                        self.status_var.set("Сбор завершён")
                    elif code is not None:
                        self.status_var.set(f"Скрипт завершился с кодом {code}; подробности — в журнале")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _finish(self, code: int | None) -> None:
        self.progress.stop()
        self.start_button.configure(state="normal")
        self.test_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if code is not None:
            self.process = None

    def _stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.status_var.set("Останавливаю…")
            self._append_log("Получен запрос на остановку.")

    def _open_output(self) -> None:
        path = Path(self.output_var.get()).expanduser()
        if not path.is_absolute():
            path = SCRIPT_DIR / path
        path.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Не удалось открыть папку", str(exc))

    def _on_close(self) -> None:
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("Сбор ещё идёт", "Остановить сбор и закрыть окно?"):
                return
            self.process.terminate()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ForumCollectorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
