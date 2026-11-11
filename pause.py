"""Pause d'un traitement : le processus et ses enfants sont gelés tels quels, puis repris là où ils en étaient.
Le calcul en cours (Whisper, séparation des voix) ne se découpe pas : seul le système peut l'arrêter net.
Windows : NtSuspendProcess / NtResumeProcess ; ailleurs : SIGSTOP / SIGCONT."""
import os
import subprocess
import sys


def descendants(pid: int) -> list[int]:
    """pid et tous ses descendants (la trampoline uv de l'environnement lance un second python)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Entree(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]

        k = ctypes.windll.kernel32
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        cliche = k.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        parents = {}
        e = Entree()
        e.dwSize = ctypes.sizeof(Entree)
        try:
            ok = k.Process32FirstW(wintypes.HANDLE(cliche), ctypes.byref(e))
            while ok:
                parents.setdefault(e.th32ParentProcessID, []).append(e.th32ProcessID)
                ok = k.Process32NextW(wintypes.HANDLE(cliche), ctypes.byref(e))
        finally:
            k.CloseHandle(wintypes.HANDLE(cliche))
    else:
        parents = {}
        sortie = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout
        for ligne in sortie.splitlines():
            p, pp = map(int, ligne.split())
            parents.setdefault(pp, []).append(p)
    tous, a_voir = [], [pid]
    while a_voir:
        p = a_voir.pop()
        if p not in tous:
            tous.append(p)
            a_voir += parents.get(p, [])
    return tous


def geler(pid: int, oui: bool = True) -> None:
    """Gèle (oui) ou reprend (non) pid et ses descendants. Un processus déjà fini est ignoré."""
    for p in descendants(pid):
        if sys.platform == "win32":
            import ctypes

            k, nt = ctypes.windll.kernel32, ctypes.windll.ntdll
            k.OpenProcess.restype = ctypes.c_void_p
            h = k.OpenProcess(0x0800, False, p)  # PROCESS_SUSPEND_RESUME
            if not h:
                continue
            try:
                (nt.NtSuspendProcess if oui else nt.NtResumeProcess)(ctypes.c_void_p(h))
            finally:
                k.CloseHandle(ctypes.c_void_p(h))
        else:
            import signal

            try:
                os.kill(p, signal.SIGSTOP if oui else signal.SIGCONT)
            except ProcessLookupError:
                pass
