"""Convert an immutable DOC work copy, then use the existing DOCX extractor.

LibreOffice is the default optional external component; it is not bundled.
Windows Word/WPS automation uses built-in PowerShell, not pywin32. It refuses
to open a document unless macro disabling and ownership of the new instance
can both be established. No running user Office instance is terminated.

The same file is an isolated stdlib-only process supervisor. It watches its
parent's pipe and owns its descendants within the worker's process boundary.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile

CONVERTERS = ("libreoffice", "word", "wps")
MAX_CONVERTED_BYTES = 64 * 1024**2
PROFILE_ALLOWANCE = 64 * 1024**2

# LibreOffice's own configuration schema defines these controls:
# https://github.com/LibreOffice/core/blob/master/officecfg/registry/schema/org/openoffice/Office/Common.xcs
# https://github.com/LibreOffice/core/blob/master/officecfg/registry/schema/org/openoffice/Office/Writer.xcs
_PROFILE = '''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
 <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
  <prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
  <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
  <prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>
  <prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop>
 </item>
 <item oor:path="/org.openoffice.Office.Writer/Content/Update">
  <prop oor:name="Link" oor:op="fuse"><value>2</value></prop>
  <prop oor:name="Field" oor:op="fuse"><value>false</value></prop>
  <prop oor:name="Chart" oor:op="fuse"><value>false</value></prop>
 </item>
</oor:items>
'''

# Paths are read from a private JSON file, never interpolated into shell code.
# AutomationSecurity=3 is msoAutomationSecurityForceDisable. DisplayAlerts alone
# does not disable macros: https://learn.microsoft.com/office/vba/api/word.application.automationsecurity
_COM_SCRIPT = r'''
param([string]$SpecPath)
$ErrorActionPreference = 'Stop'
$spec = Get-Content -LiteralPath $SpecPath -Raw -Encoding UTF8 | ConvertFrom-Json
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class DocNative {
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern IntPtr OpenJobObject(uint access, bool inherit, string name);
 [DllImport("kernel32.dll", SetLastError=true)] public static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
 [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);
}
'@
$app = $null; $document = $null; $owned = $false; $jobHandle = [IntPtr]::Zero
$savedOptions = @{}
$stage = 'activation'
try {
 $before = @(Get-Process | Select-Object -ExpandProperty Id)
 $app = New-Object -ComObject $spec.progid
 $stage = 'ownership'
 [uint32]$officePid = 0
 [void][DocNative]::GetWindowThreadProcessId([IntPtr]$app.Hwnd, [ref]$officePid)
 if ($officePid -eq 0 -or $before -contains [int]$officePid -or $app.UserControl) { throw 'unowned_instance' }
 $officeProcess = Get-Process -Id $officePid
 if ($officeProcess.ProcessName.ToLowerInvariant() -notin @('winword','wps','kwps')) { throw 'unexpected_process' }
 $owned = $true
 $jobHandle = [DocNative]::OpenJobObject(1, $false, [string]$spec.job_name)
 if ($jobHandle -eq [IntPtr]::Zero -or -not [DocNative]::AssignProcessToJobObject($jobHandle, $officeProcess.Handle)) { throw 'job_assignment_failed' }
 [void][DocNative]::CloseHandle($jobHandle); $jobHandle = [IntPtr]::Zero
 $stage = 'security'
 $app.AutomationSecurity = 3
 if ([int]$app.AutomationSecurity -ne 3) { throw 'macro_disabling_unavailable' }
 $app.Visible = $false
 $app.DisplayAlerts = 0
 foreach ($option in @('UpdateLinksAtOpen', 'SaveNormalPrompt')) {
  $savedOptions[$option] = $app.Options.$option
  $app.Options.$option = $false
 }
 $stage = 'open'
 # ConfirmConversions=false, ReadOnly=true, AddToRecentFiles=false. The fixed
 # nonempty password prevents interactive password prompts on protected files.
 $missing = [Type]::Missing
 $document = $app.Documents.Open([string]$spec.input, $false, $true, $false,
   'yuecang-no-password', 'yuecang-no-password', $false, $missing, $missing,
   $missing, $missing, $false, $false, $missing, $true, $missing)
 if (-not $document.ReadOnly) { throw 'readonly_not_honored' }
 $stage = 'save'
 $document.SaveAs2([string]$spec.output, 16)
 [IO.File]::WriteAllText([string]$spec.com_result, '{"ok":true}', [Text.UTF8Encoding]::new($false))
} catch {
 # Exception messages may contain document text or paths; return only stage.
 [IO.File]::WriteAllText([string]$spec.com_result, ('{"ok":false,"stage":"' + $stage + '"}'), [Text.UTF8Encoding]::new($false))
 exit 1
} finally {
 if ($document) { try { $document.Close(0) } catch {} }
 if ($owned -and $app) {
  foreach ($option in $savedOptions.Keys) { try { $app.Options.$option = $savedOptions[$option] } catch {} }
  try { $app.Quit(0) } catch {}
 }
 if ($document) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) }
 if ($app) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($app) }
 if ($jobHandle -ne [IntPtr]::Zero) { [void][DocNative]::CloseHandle($jobHandle) }
}
'''


def _libreoffice_path(configured=None):
    configured = configured or os.environ.get("YUECANG_LIBREOFFICE")
    candidates = [configured] if configured else [shutil.which("libreoffice"), shutil.which("soffice")]
    if not configured and os.name == "nt":
        candidates += [str(Path(os.environ.get(key, "C:/Program Files")) / "LibreOffice/program/soffice.com")
                       for key in ("ProgramFiles", "ProgramFiles(x86)")]
    for candidate in candidates:
        if not candidate:
            continue
        found = shutil.which(str(candidate)) or str(candidate)
        path = Path(found)
        if path.is_file() and (os.name != "nt" or path.suffix.lower() in {".exe", ".com"}):
            return str(path.resolve())
    return None


def _powershell_path():
    if os.name != "nt":
        return None
    path = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    return str(path) if path.is_file() else None


def _com_progid(converter):
    if os.name != "nt":
        return None
    import winreg
    for progid in (("Word.Application",) if converter == "word" else ("Kwps.Application", "Wps.Application")):
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + "\\CLSID") as key:
                if winreg.QueryValueEx(key, None)[0]:
                    return progid
        except OSError:
            pass
    return None


def detect_converters(libreoffice_path=None):
    """Read filesystem/registration only; never instantiate Office applications."""
    return [{"id": converter, "available": bool(_libreoffice_path(libreoffice_path)) if converter == "libreoffice"
             else bool(_powershell_path() and _com_progid(converter)),
             "portable_included": False,
             "reason": "需要本机安装 LibreOffice" if converter == "libreoffice" else "需要 Windows、本机 Office COM 注册及可隔离的自动化实例"}
            for converter in CONVERTERS]


def _hash_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _linked(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def extract_doc(path, resource_dir, *, converter="auto", priority=("libreoffice",),
                libreoffice_path=None, timeout_seconds=120, cancel_check=None):
    """Convert one work copy. Auto defaults to LibreOffice only, with no retries.

    ``priority`` is an explicit ordered list used only by auto selection. A
    selected converter's failure is reported, never silently retried elsewhere.
    ``cancel_check`` returns True to cancel, or can raise a caller's stale-lease
    exception; either path stops the owned converter before returning/raising.
    """
    from .common import ExtractionError, ExtractionResult, MAX_INPUT_BYTES, resource_bytes, temporary_workspace
    from .docx import extract_docx
    path = Path(path)
    result = ExtractionResult(path.stem, parser="doc-conversion-v1", metadata={"source_format": "doc"})

    def fail(code, message, **extra):
        result.issue(code, message, severity="error", **extra)
        return result

    if converter not in ("auto", *CONVERTERS) or not isinstance(priority, (list, tuple)) or not priority or len(priority) > 3 or any(x not in CONVERTERS for x in priority):
        raise ValueError("无效的 DOC 转换器或优先级")
    if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0.1 <= timeout_seconds <= 900:
        raise ValueError("DOC 转换超时必须在 0.1 至 900 秒之间")
    if not path.is_file() or _linked(path):
        raise ExtractionError("source_not_a_regular_file")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ExtractionError("source_size_limit")
    with path.open("rb") as stream:
        signature = stream.read(8)
    if not (signature == bytes.fromhex("d0cf11e0a1b11ae1") or signature.startswith(b"{\\rtf")):
        return fail("doc_format_invalid", "文件不是可识别的 DOC/OLE 或 RTF 内容；原件已保留")
    available = {item["id"]: item["available"] for item in detect_converters(libreoffice_path)}
    selected = next((item for item in priority if available[item]), None) if converter == "auto" else converter
    if not selected or not available[selected]:
        return fail("doc_converter_unavailable", "所选 DOC 转换器未安装或不可用；请安装 LibreOffice 或明确选择本机 Word/WPS", converter=converter)
    source_sha = _hash_file(path)
    result.metadata["conversion"] = {"converter": selected, "output_format": "docx", "source_sha256": source_sha,
                                     "macros": "disabled", "work_copy": "read_only", "timeout_seconds": timeout_seconds}
    if cancel_check and cancel_check():
        return fail("doc_conversion_cancelled", "DOC 转换已取消，原件未修改")
    root = Path(resource_dir).resolve() if resource_dir is not None else Path(tempfile.mkdtemp(prefix="yuecang-doc-resources-"))
    maximum = path.stat().st_size + MAX_CONVERTED_BYTES + PROFILE_ALLOWANCE
    with temporary_workspace(root, maximum=maximum, prefix="doc-convert-") as work:
        local = work / "source.doc"
        shutil.copyfile(path, local)
        if _hash_file(local) != source_sha:
            return fail("doc_source_changed", "复制期间原件发生变化，已拒绝本次结果")
        out = work / "output"
        out.mkdir()
        output = out / "source.docx"
        profile = work / "profile" / "user"
        profile.mkdir(parents=True)
        (profile / "registrymodifications.xcu").write_text(_PROFILE, encoding="utf-8")
        spec = {"converter": selected, "work": str(work.resolve()), "input": str(local.resolve()), "output": str(output.resolve()),
                "timeout": timeout_seconds, "maximum": maximum, "job_name": "Local\\YueCangDoc-" + uuid.uuid4().hex}
        if selected == "libreoffice":
            spec["command"] = [_libreoffice_path(libreoffice_path), "-env:UserInstallation=" + profile.parent.resolve().as_uri(),
                               "--headless", "--nologo", "--nodefault", "--norestore", "--convert-to", "docx:Office Open XML Text",
                               "--outdir", str(out.resolve()), str(local.resolve())]
        else:
            helper = work / "convert.ps1"
            helper.write_text(_COM_SCRIPT, encoding="utf-8-sig")
            spec.update(progid=_com_progid(selected), com_result=str(work / "com-result.json"))
            spec["command"] = [_powershell_path(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(helper.resolve()), "-SpecPath", str(work / "spec.json")]
        local.chmod(stat.S_IREAD)
        try:
            outcome = _run_supervisor(work, spec, cancel_check)
            if outcome["status"] != "ok":
                messages = {"timeout": "DOC 转换超时，已停止本次转换进程", "cancelled": "DOC 转换已取消，原件未修改",
                            "limit": "DOC 转换临时内容超过空间上限，已停止", "isolation": "无法建立独立转换进程边界，未打开文档",
                            "failed": "DOC 转换失败；可能为加密、损坏或转换器不支持的内容，原件已保留"}
                return fail("doc_conversion_" + outcome["status"], messages.get(outcome["status"], messages["failed"]),
                            converter=selected, **({"stage": outcome["stage"]} if outcome.get("stage") else {}))
            if _hash_file(path) != source_sha:
                return fail("doc_source_changed", "转换期间原件发生变化，已拒绝本次结果")
            if not output.is_file() or _linked(out) or _linked(output) or not 0 < output.stat().st_size <= MAX_CONVERTED_BYTES:
                return fail("doc_conversion_output", "转换器未生成有效大小的 DOCX，原件已保留")
            try:
                extracted = extract_docx(output, root)
            except (ExtractionError, zipfile.BadZipFile, KeyError, UnicodeError):
                return fail("doc_conversion_output", "转换得到的 DOCX 结构无效或超出处理上限，原件已保留")
            if any(issue["severity"] == "error" for issue in extracted.issues):
                return fail("doc_conversion_output", "转换得到的 DOCX 正文结构无效，原件已保留")
            artifact = resource_bytes(output.read_bytes(), "converted.docx", root,
                                      mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            if extracted.title == output.stem and not extracted.metadata.get("title"):
                extracted.title = path.stem
            extracted.metadata.update(result.metadata)
            extracted.metadata["conversion"]["converted_sha256"] = artifact["sha256"]
            extracted.metadata["conversion"]["extractor"] = extracted.parser
            extracted.parser = "doc-conversion-v1"
            extracted.add("unknown", "", {"role": "converted_document"}, resources=[artifact], attributes={"role": "conversion_artifact"})
            extracted.issue("doc_conversion_review_required", "DOC 已转换为 DOCX；复杂排版、域、嵌入对象和宏可能变化，请对照原件核对", converter=selected)
            return extracted
        finally:
            # Read-only copies must become removable on Windows. The immutable
            # source is never chmod'ed, opened by Office or passed on its CLI.
            if local.exists() and not _linked(local) and local.stat().st_nlink == 1:
                local.chmod(stat.S_IREAD | stat.S_IWRITE)


def _run_supervisor(work, spec, cancel_check):
    spec_path = work.resolve() / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    # Keep the helper in the worker's session, but let it survive a signal to
    # the worker's group so it can clean up any detached converter descendants.
    process_group = 0 if sys.platform.startswith("linux") else None
    try:
        process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--supervise", str(spec_path)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   cwd=work, close_fds=True, creationflags=flags, process_group=process_group)
    except OSError:
        return {"status": "isolation"}
    stop_reason = None
    try:
        deadline = time.monotonic() + spec["timeout"] + 10
        while process.poll() is None:
            if cancel_check and cancel_check():
                stop_reason = "cancelled"
                break
            if time.monotonic() > deadline:
                stop_reason = "timeout"
                break
            time.sleep(0.05)
    finally:
        if not process.stdin.closed:
            process.stdin.close()
        # The helper drains its exact owned tree before exit.
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            if sys.platform.startswith("linux"):
                _stop_linux_children(process.pid, reap=False)
            process.kill()
            process.wait(timeout=3)
        finally:
            with process.stdout:
                raw = b""
                if process.returncode is not None:
                    raw = process.stdout.read(1024)
    if stop_reason or (cancel_check and cancel_check()):
        return {"status": stop_reason or "cancelled"}
    try:
        return json.loads(raw)
    except ValueError:
        return {"status": "failed"}


def _linux_stamp(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _linux_children(pid):
    try:
        direct = [int(x) for x in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()]
    except OSError:
        return []
    descendants = []
    for child in direct:
        descendants.extend(_linux_children(child))
        descendants.append((child, _linux_stamp(child)))
    return descendants


def _stop_linux_children(owner=None, *, reap=True):
    # The supervisor is a subreaper: detached/reparented converter children stay
    # owned by it. PID birth stamps prevent terminating a reused PID.
    for _ in range(20):
        children = _linux_children(owner if owner is not None else os.getpid())
        if not children:
            break
        for pid, stamp in children:
            if stamp is not None and _linux_stamp(pid) == stamp:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        while reap:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        time.sleep(0.02)


def _windows_job(name):
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64), ("flags", wintypes.DWORD),
                    ("min_set", ctypes.c_size_t), ("max_set", ctypes.c_size_t), ("active", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6), ("memory", ctypes.c_size_t * 4)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, name)
    limits = Extended()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        if handle:
            kernel.CloseHandle(handle)
        raise OSError("process_isolation_unavailable")
    return kernel, handle


def _directory_size(root):
    total = 0
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in folders + files:
            path = Path(directory) / name
            try:
                if _linked(path):
                    raise OSError("converter_created_link")
                if path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass
    return total


def _supervise(spec_path):
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    work = Path(spec["work"])
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    def watch_parent():
        os.read(sys.stdin.fileno(), 1)
        stopped.set()
    threading.Thread(target=watch_parent, daemon=True).start()
    outcome, process, job = {"status": "isolation"}, None, None
    try:
        if os.name == "nt":
            job = _windows_job(spec["job_name"])
        elif sys.platform.startswith("linux"):
            if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
                raise OSError("subreaper_unavailable")
        else:
            raise OSError("process_isolation_unsupported")
        environment = os.environ.copy()
        for key, folder in (("TMPDIR", "temp"), ("TMP", "temp"), ("TEMP", "temp"), ("XDG_CACHE_HOME", "cache")):
            target = work / folder
            target.mkdir(exist_ok=True)
            environment[key] = str(target)
        if stopped.is_set():
            outcome = {"status": "cancelled"}
            return 0
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(spec["command"], cwd=work, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
        deadline = time.monotonic() + spec["timeout"]
        while process.poll() is None:
            if stopped.wait(0.05):
                outcome = {"status": "cancelled"}
                break
            if time.monotonic() >= deadline:
                outcome = {"status": "timeout"}
                break
            if _directory_size(work) > spec["maximum"]:
                outcome = {"status": "limit"}
                break
        else:
            outcome = {"status": "ok" if process.returncode == 0 else "failed"}
        if _directory_size(work) > spec["maximum"]:
            outcome = {"status": "limit"}
        if outcome["status"] == "failed" and spec.get("com_result"):
            com_result = Path(spec["com_result"])
            if com_result.is_file() and not _linked(com_result) and com_result.stat().st_size <= 1024:
                automation = json.loads(com_result.read_text(encoding="utf-8-sig"))
                stage = automation.get("stage")
                if stage in {"activation", "ownership", "security", "open", "save"}:
                    outcome["stage"] = stage
    except Exception:
        if process is not None:
            outcome = {"status": "failed"}
    finally:
        if os.name != "nt":
            _stop_linux_children()
        # The converter never inherits this private pipe. Its working directory
        # cannot supply or redirect the supervisor's outcome.
        try:
            sys.stdout.write(json.dumps(outcome))
            sys.stdout.flush()
        except BrokenPipeError:
            pass
        if job:
            # Closing also terminates this helper; write the bounded result first.
            job[0].CloseHandle(job[1])
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--supervise":
        raise SystemExit(2)
    raise SystemExit(_supervise(sys.argv[2]))
