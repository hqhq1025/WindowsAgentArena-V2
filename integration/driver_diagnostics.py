"""Export only bridge-owned diagnostic filenames before an owned guest stops."""
import base64
import json
from pathlib import Path
import re
import time
import uuid


GUEST_ROOT = r"C:\waa-bridge\driver-diagnostics"
SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
LIMITS = {"stderr.log": 1024 * 1024, "status.json": 16384}
MAX_SESSIONS = 64


def guest_script(session_id=None):
    if session_id is not None and not SESSION.fullmatch(session_id):
        raise ValueError("Invalid bridge session ID")
    return r'''
import base64,itertools,json,re
from pathlib import Path
root=Path(''' + repr(GUEST_ROOT) + ''')
session_id=''' + repr(session_id) + '''
limits=''' + repr(LIMITS) + '''
pattern=re.compile(''' + repr(SESSION.pattern) + r''')
def collect():
 if not root.exists(): return {'status':'missing','sessions':[]}
 if not root.is_dir() or root.resolve()!=root: return {'status':'unsafe_root','sessions':[]}
 if session_id is None:
  children=list(itertools.islice(root.iterdir(),65))
  sessions=[p.name for p in children[:64] if pattern.fullmatch(p.name) and p.is_dir() and p.resolve()==p]
  return {'status':'ok' if len(children)<=64 and len(sessions)==len(children) else 'incomplete', 'sessions':sorted(sessions)}
 directory=root/session_id
 if not directory.is_dir() or directory.resolve()!=directory: return {'status':'missing_session'}
 files={}
 for name,limit in limits.items():
  path=directory/name
  if not path.is_file(): files[name]={'status':'missing'}; continue
  if path.resolve()!=path: files[name]={'status':'unsafe_path'}; continue
  with path.open('rb') as stream: data=stream.read(limit+1)
  files[name]={'status':'oversized'} if len(data)>limit else {'status':'saved','data':base64.b64encode(data).decode('ascii')}
 return {'status':'ok','files':files}
print(json.dumps(collect()))
'''


def episode_identity(binding, directory):
    directory = Path(directory).resolve()
    if Path(binding["storage"]).resolve() != directory / "storage":
        raise ValueError("Diagnostic storage does not belong to this episode directory")
    return {"episode_id": binding["episode_id"], "container": binding["container"],
            "storage": str(directory / "storage"), "episode_directory": str(directory)}


def export_diagnostics(binding, directory, execute, container_id, timeout=30):
    """A failed export preserves storage and cannot become a successful seal."""
    identity = episode_identity(binding, directory)
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ValueError("Diagnostic export requires the full owned container ID")
    root = Path(identity["episode_directory"]) / "driver-diagnostic-captures" / str(uuid.uuid4())
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {**identity, "container_id": container_id, "guest_root": GUEST_ROOT,
              "status": "failed", "export_complete": False,
              "diagnostics_complete": False, "sessions": [], "files": [],
              "receipt": str(root / "export.json")}
    deadline = time.monotonic() + timeout

    def fetch(session_id=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Diagnostic export budget exhausted")
        result = execute(guest_script(session_id), timeout=remaining)
        if result.get("returncode") != 0:
            raise RuntimeError("Guest diagnostic read failed")
        return json.loads(result["output"])

    try:
        inventory = fetch()
        sessions = inventory.get("sessions", [])
        if (not isinstance(sessions, list) or len(sessions) > MAX_SESSIONS
                or len(set(sessions)) != len(sessions)
                or any(not isinstance(s, str) or not SESSION.fullmatch(s) for s in sessions)):
            raise ValueError("Invalid guest diagnostic inventory")
        report["inventory_status"] = inventory.get("status")
        complete = inventory.get("status") == "ok"
        all_diagnostics = complete
        for session_id in sessions:
            item = {"session_id": session_id, "status": "failed"}
            report["sessions"].append(item)
            snapshot = fetch(session_id)
            files = snapshot.get("files", {})
            state = None
            saved = {}
            for name, limit in LIMITS.items():
                entry = files.get(name, {})
                if entry.get("status") != "saved":
                    item[name] = entry.get("status", "unavailable")
                    complete = False
                    continue
                encoded = entry.get("data")
                if not isinstance(encoded, str) or len(encoded) > 4 * ((limit + 2) // 3):
                    raise ValueError("Diagnostic file exceeds its transfer limit")
                data = base64.b64decode(encoded, validate=True)
                if len(data) > limit:
                    raise ValueError("Diagnostic file exceeds its byte limit")
                target = root / session_id / name
                target.parent.mkdir(mode=0o700, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(data)
                if target.read_bytes() != data:
                    raise OSError("Diagnostic file readback differs")
                report["files"].append({"path": str(target.relative_to(root)), "bytes": len(data)})
                item[name] = "saved"
                saved[name] = data
            if "status.json" in saved:
                state = json.loads(saved["status.json"])
                item["capture_status"] = state
            closed = (isinstance(state, dict) and state.get("reader_done") is True
                      and state.get("session_closed") is True
                      and isinstance(state.get("saved_bytes"), int)
                      and state["saved_bytes"] == len(saved.get("stderr.log", b""))
                      and "stderr.log" in saved)
            complete = complete and closed
            item["status"] = "exported" if closed else "incomplete"
            all_diagnostics = (all_diagnostics and closed and state.get("truncated") is False
                               and state.get("errors") == [])
        if fetch() != inventory:
            complete = False
            report["inventory_changed"] = True
        report.update(export_complete=complete, diagnostics_complete=all_diagnostics and complete,
                      status=("exported" if sessions else "no_sessions") if complete else "incomplete")
    except Exception as exc:
        # Do not copy controller error bodies, credentials, or driver output into errors.
        report["error_type"] = type(exc).__name__
    (root / "export.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def reusable_export(report, binding, directory):
    """An absent VM may reuse a complete prior export, never a failed recapture."""
    if not isinstance(report, dict) or report.get("export_complete") is not True:
        return False
    try:
        identity = episode_identity(binding, directory)
        if any(report.get(key) != value for key, value in identity.items()):
            return False
        if not re.fullmatch(r"[0-9a-f]{64}", report.get("container_id", "")):
            return False
        if binding.get("container_id", report["container_id"]) != report["container_id"]:
            return False
        receipt = Path(report["receipt"])
        expected_root = Path(identity["episode_directory"]) / "driver-diagnostic-captures"
        if (receipt.name != "export.json" or not SESSION.fullmatch(receipt.parent.name)
                or receipt.parent.parent != expected_root or receipt.resolve() != receipt):
            return False
        saved = json.loads(receipt.read_text())
        if saved != report:
            return False
        for entry in report["files"]:
            relative = Path(entry["path"])
            if (len(relative.parts) != 2 or not SESSION.fullmatch(relative.parts[0])
                    or relative.parts[1] not in LIMITS):
                return False
            target = receipt.parent / relative
            if (target.resolve() != receipt.parent.resolve() / relative
                    or target.stat().st_size != entry["bytes"]):
                return False
        return True
    except (OSError, KeyError, TypeError, ValueError):
        return False
