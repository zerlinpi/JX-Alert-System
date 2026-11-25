"""Official SparkX Skills, installed from SparkXAI/SparkXAI-MCP the way an agent installs them.

"MCP 的 Tool 决定 AI 能拿到什么数据，Skill 决定 AI 把数据用得好不好" (SparkX README).
Every Skill whose MCP tools are all read tools is installed: the three query Skills
and the read-only analyses (weekly/monthly report, structure, product, ASIN and
search-term diagnosis, market data, category scorecard). Skills that need a write
tool are listed in the console with the reason but never installed. Files are kept
under ``settings.sparkx_skills_dir`` in a versioned folder that an index file points
to; DeepSeek reads them through the ``read_skill`` tool, as Claude/Codex load SKILL.md
and its references on demand. Nothing is copied into this repository.

The owner switches optional Skills on and off in the console (``choices.json``); the
official required Skills (the query Skills) always stay on. A run offers DeepSeek only
the switched-on Skills whose tools SparkX offers this token.
"""
from datetime import datetime, timezone
import io
import json
import logging
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import tarfile
import threading
import time

import httpx

from jx_alert.sparkx.client import READ_TOOLS
from jx_alert.storage import private_file

_LOG = logging.getLogger(__name__)
REPOSITORY = "SparkXAI/SparkXAI-MCP"
ARCHIVE_URL = "https://codeload.github.com/SparkXAI/SparkXAI-MCP/tar.gz/{ref}"
MANIFEST_URL = "https://raw.githubusercontent.com/SparkXAI/SparkXAI-MCP/{ref}/skills/manifest.json"
MAX_ARCHIVE = 40 * 1024 * 1024
MAX_FILE = 1024 * 1024
MAX_TOTAL = 24 * 1024 * 1024
TEXT_SUFFIXES = {".md", ".json", ".txt", ".yaml", ".yml", ".csv"}
READ_CHUNK = 40_000
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,120}){0,4}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,80}")
_AUTO_RETRY = 30 * 60
_auto_lock = threading.Lock()
_auto_attempt = {"at": 0.0}


class SkillsError(RuntimeError):
    def __init__(self, detail="SparkX Skills 无法安装，请稍后重试。", status_code=502):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


def _root(settings):
    return Path(getattr(settings, "sparkx_skills_dir", "/app/data/sparkx-skills"))


def _index_path(settings):
    return _root(settings) / "installed.json"


def installed(settings):
    """The active index, or None when nothing (valid) is installed."""
    try:
        data = private_file.read_json(_index_path(settings), max_bytes=512 * 1024)
    except ValueError:
        return None
    if (not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("folder"), str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", data["folder"]) or not isinstance(data.get("skills"), list)):
        return None
    if not (_root(settings) / data["folder"]).is_dir():
        return None
    return data


def catalog(manifest):
    """Every well-formed manifest entry, with ``installable`` (all tools are read tools) and the others."""
    skills = manifest.get("skills") if isinstance(manifest, dict) else None
    if not isinstance(skills, list):
        raise SkillsError("SparkX Skills 清单格式无法识别。")
    found = []
    for entry in skills:
        if not isinstance(entry, dict):
            continue
        name, path, tools = entry.get("name"), entry.get("path"), entry.get("mcp_tools")
        if (isinstance(name, str) and _NAME.fullmatch(name) and isinstance(path, str) and _NAME.fullmatch(path)
                and isinstance(tools, list) and tools and all(isinstance(tool, str) for tool in tools)):
            tools = [tool[:80] for tool in tools[:40]]
            found.append({"name": name, "path": path, "version": str(entry.get("version") or "")[:20],
                          "description": str(entry.get("description") or "")[:600], "mcp_tools": tools,
                          "required": entry.get("required") is True, "installable": set(tools) <= READ_TOOLS,
                          "blocked_tools": [tool for tool in tools if tool not in READ_TOOLS]})
    return found


def selected(manifest):
    """Manifest entries whose tools are all read tools (no write Skill is ever installed)."""
    return [{key: value for key, value in entry.items() if key not in {"installable", "blocked_tools"}}
            for entry in catalog(manifest) if entry["installable"]]


# ---------------------------------------------------------------- the owner's choices

def _choices_path(settings):
    return _root(settings) / "choices.json"


_EMPTY = "0" * 64


def choices(settings):
    """{"revision", "disabled": [names], "updated_at"}: optional Skills the owner switched off."""
    try:
        data = private_file.read_json(_choices_path(settings), max_bytes=64 * 1024)
    except ValueError:
        raise SkillsError("Skills 启用设置无法读取，请检查 data 目录权限。", 503) from None
    if data is None:
        return {"revision": _EMPTY, "disabled": [], "updated_at": None}
    disabled = data.get("disabled") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("revision"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", data["revision"]) or not isinstance(disabled, list)
            or not all(isinstance(name, str) and _NAME.fullmatch(name) for name in disabled)):
        raise SkillsError("Skills 启用设置无法读取，请检查 data 目录权限。", 503)
    return {"revision": data["revision"], "disabled": sorted(set(disabled)), "updated_at": data.get("updated_at")}


def save_choices(settings, disabled, revision):
    """Switch optional installed Skills off (the others on). Required Skills cannot be switched off."""
    index = installed(settings)
    if not index:
        raise SkillsError("请先安装官方 Skills。", 409)
    names = {entry["name"]: entry for entry in index["skills"]}
    if (not isinstance(disabled, list) or len(disabled) > 100
            or not all(isinstance(name, str) and name in names for name in disabled)):
        raise SkillsError("选择的 Skill 无效，请刷新后重试。", 400)
    if any(names[name].get("required") for name in disabled):
        raise SkillsError("基础查询 Skill 不能停用：DeepSeek 查询 SparkX 前必须读取它们。", 400)
    path = _choices_path(settings)
    try:
        with private_file.locked(path):
            if choices(settings)["revision"] != revision:
                raise SkillsError("Skills 设置已更新，请刷新后重新选择。", 409)
            candidate = {"version": 1, "revision": secrets.token_hex(32), "disabled": sorted(set(disabled)),
                         "updated_at": datetime.now(timezone.utc).isoformat()}
            private_file.replace_json(path, candidate)
    except private_file.Busy:
        raise SkillsError("Skills 设置正在保存，请稍后刷新。", 409) from None
    except (OSError, ValueError):
        raise SkillsError("Skills 启用设置无法保存，请检查 data 目录权限。", 503) from None
    return candidate


def _keep(index, names):
    return {**index, "skills": [entry for entry in index["skills"] if entry["name"] in names]}


def for_agent(settings):
    """The installed index without the Skills the owner switched off (required Skills always stay)."""
    index = ensure(settings)
    if not index:
        return index
    try:
        off = set(choices(settings)["disabled"])
    except SkillsError:
        off = set()  # unreadable choices: offer every installed Skill rather than none
    return _keep(index, {entry["name"] for entry in index["skills"] if entry.get("required") or entry["name"] not in off})


def usable(index, tool_names):
    """Only the Skills whose MCP tools are all offered in this session (e.g. market data needs extra scopes)."""
    if not index or not tool_names:
        return index
    offered = set(tool_names)
    return _keep(index, {entry["name"] for entry in index["skills"] if set(entry.get("mcp_tools") or ()) <= offered})


def _download(ref, transport):
    url = ARCHIVE_URL.format(ref=ref)
    try:
        with httpx.Client(timeout=httpx.Timeout(60.0), follow_redirects=False, trust_env=False,
                          transport=transport) as client:
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise SkillsError(f"下载 SparkX Skills 失败（GitHub 返回 {response.status_code}）。")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_ARCHIVE:
                        raise SkillsError("SparkX Skills 压缩包过大，已停止下载。")
                return bytes(body)
    except httpx.HTTPError:
        raise SkillsError("服务器无法连接 GitHub（codeload.github.com），SparkX Skills 未更新。") from None


def _extract(archive):
    """(manifest, {skill path: {relative file: text}}, commit) from the GitHub tarball, read in memory."""
    try:
        bundle = tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError):
        raise SkillsError("SparkX Skills 压缩包无法读取。") from None
    with bundle:
        commit = bundle.pax_headers.get("comment") if isinstance(bundle.pax_headers, dict) else None
        files, manifest, total = {}, None, 0
        for member in bundle.getmembers():
            parts = PurePosixPath(member.name).parts
            if len(parts) < 3 or parts[1] != "skills" or ".." in parts or not member.isfile():
                continue
            if member.size > MAX_FILE:
                continue
            if parts[2:] == ("manifest.json",):
                stream = bundle.extractfile(member)
                try:
                    manifest = json.loads(stream.read().decode("utf-8")) if stream else None
                except (ValueError, UnicodeDecodeError):
                    raise SkillsError("SparkX Skills 清单无法解析。") from None
                continue
            if len(parts) < 4 or PurePosixPath(member.name).suffix.lower() not in TEXT_SUFFIXES:
                continue
            relative = "/".join(parts[3:])
            if not _FILE.fullmatch(relative):
                continue
            total += member.size
            if total > MAX_TOTAL:
                raise SkillsError("SparkX Skills 内容超出大小上限。")
            stream = bundle.extractfile(member)
            if stream is None:
                continue
            try:
                files.setdefault(parts[2], {})[relative] = stream.read().decode("utf-8")
            except UnicodeDecodeError:
                continue
    if manifest is None:
        raise SkillsError("压缩包中没有 skills/manifest.json。")
    sha = commit.strip() if isinstance(commit, str) and re.fullmatch(r"\s*[0-9a-f]{40}\s*", commit) else None
    return manifest, files, sha


def install(settings, *, ref="main", transport=None):
    """Download the official repository archive and switch to its read-only Skills."""
    if not isinstance(ref, str) or not _REF.fullmatch(ref) or ".." in ref:
        raise SkillsError("Skills 版本参数无效。", 400)
    manifest, files, commit = _extract(_download(ref, transport))
    chosen = [entry for entry in selected(manifest) if "SKILL.md" in files.get(entry["path"], {})]
    if not chosen:
        raise SkillsError("官方清单中没有可安装的只读 Skill。")
    listed = [{key: value for key, value in entry.items() if key != "path"} for entry in catalog(manifest)]
    release = str(manifest.get("release") or "")[:20]
    root = _root(settings)
    folder = re.sub(r"[^A-Za-z0-9._-]", "-", f"{release or ref}-{(commit or secrets.token_hex(4))[:7]}")[:60]
    folder += "-" + secrets.token_hex(3)
    staging = root / ("." + folder)
    try:
        root.mkdir(parents=True, exist_ok=True)
        with private_file.locked(_index_path(settings)):
            previous = installed(settings)
            for entry in chosen:
                for relative, text in files[entry["path"]].items():
                    target = staging / entry["name"] / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text, encoding="utf-8")
                entry["files"] = sorted(files[entry["path"]])
                entry.pop("path")
            staging.rename(root / folder)
            index = {"version": 1, "source": REPOSITORY, "ref": ref, "commit": commit, "release": release,
                     "manifest_updated_at": str(manifest.get("updated_at") or "")[:20],
                     "installed_at": datetime.now(timezone.utc).isoformat(), "folder": folder, "skills": chosen,
                     "catalog": listed}
            private_file.replace_json(_index_path(settings), index)
            if previous and previous["folder"] != folder:
                shutil.rmtree(root / previous["folder"], ignore_errors=True)
    except private_file.Busy:
        raise SkillsError("Skills 正在由另一个进程安装，请稍后刷新。", 409) from None
    except (OSError, ValueError):
        raise SkillsError("Skills 无法写入运行目录，请检查 data 目录权限。", 503) from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _LOG.info("SparkX Skills installed: release %s, %d skills", release or ref, len(chosen))
    return index


def ensure(settings, *, transport=None):
    """The installed index; installs once (then at most every 30 minutes) when missing."""
    index = installed(settings)
    if index is not None:
        return index
    with _auto_lock:
        if time.monotonic() - _auto_attempt["at"] < _AUTO_RETRY and _auto_attempt["at"]:
            return None
        _auto_attempt["at"] = time.monotonic()
    try:
        return install(settings, transport=transport)
    except SkillsError as exc:
        _LOG.warning("SparkX Skills auto-install failed: %s", exc.detail)
        return None


def latest_manifest(*, ref="main", transport=None):
    """The official manifest (for the console's update check)."""
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False, trust_env=False, transport=transport) as client:
            response = client.get(MANIFEST_URL.format(ref=ref))
        if response.status_code != 200 or len(response.content) > 512 * 1024:
            raise SkillsError(f"读取官方清单失败（GitHub 返回 {response.status_code}）。")
        return response.json()
    except httpx.HTTPError:
        raise SkillsError("服务器无法连接 GitHub，无法检查 Skills 更新。") from None
    except ValueError:
        raise SkillsError("官方清单无法解析。") from None


def _state(entry, installed_names, off, offered):
    """(state, missing tools) of one catalog entry for the console."""
    if not entry["installable"]:
        return "write", entry.get("blocked_tools") or []
    if entry["name"] not in installed_names:
        return "not_installed", []
    if not entry.get("required") and entry["name"] in off:
        return "off", []
    missing = [tool for tool in entry["mcp_tools"] if offered is not None and tool not in offered]
    return ("missing_tools" if missing else "on"), missing


def status(settings, *, check=False, transport=None, offered=None):
    """Console view: installed Skills, the whole official catalog with each Skill's state, update check.

    ``offered`` (tool names SparkX offers this token, or None when unknown) shows which switched-on
    Skills cannot be used yet. States: on, off, missing_tools, not_installed, write (never installed).
    """
    index = installed(settings)
    view = {"installed": None, "skills": [], "catalog": [], "choices": None, "latest": None, "error": None,
            "offered_tools": sorted(offered) if offered is not None else None}
    try:
        current = choices(settings)
    except SkillsError as exc:
        current, view["error"] = {"revision": _EMPTY, "disabled": []}, exc.detail
    view["choices"] = {"revision": current["revision"], "disabled": current["disabled"]}
    if index:
        view["installed"] = {key: index.get(key) for key in ("release", "commit", "ref", "installed_at",
                                                             "manifest_updated_at")}
        view["skills"] = [{key: entry.get(key) for key in ("name", "version", "description", "mcp_tools", "required")}
                          for entry in index["skills"]]
        listed = index.get("catalog") or [{**entry, "installable": True, "blocked_tools": []} for entry in index["skills"]]
        names, off = {entry["name"] for entry in index["skills"]}, set(current["disabled"])
        for entry in listed:
            state, tools = _state(entry, names, off, offered)
            view["catalog"].append({key: entry.get(key) for key in ("name", "version", "description", "mcp_tools",
                                                                    "required")} | {"state": state, "tools": tools})
    if check:
        try:
            manifest = latest_manifest(transport=transport)
            versions = {entry["name"]: entry["version"] for entry in selected(manifest)}
            current = {entry["name"]: entry.get("version") for entry in view["skills"]}
            view["latest"] = {"release": str(manifest.get("release") or "")[:20],
                              "updated_at": str(manifest.get("updated_at") or "")[:20],
                              "outdated": sorted(name for name, version in versions.items()
                                                 if current.get(name) != version)}
        except SkillsError as exc:
            view["error"] = exc.detail
    return view


def prompt_index(index):
    """The Skill list placed in the agent's system prompt (name, version, description, files)."""
    if not index:
        return ""
    lines = []
    for entry in index["skills"]:
        files = [name for name in entry.get("files", []) if name != "SKILL.md"]
        shown = "、".join(files[:12]) + ("…" if len(files) > 12 else "")
        lines.append(f"- {entry['name']} {entry.get('version') or ''}（工具：{'、'.join(entry['mcp_tools'])}）："
                     f"{entry.get('description') or ''}" + (f"\n  参考文件：{shown}" if shown else ""))
    return "\n".join(lines)


def read(settings, index, skill, file="SKILL.md", offset=0):
    """Text of one installed Skill file, in chunks; the agent's read_skill tool."""
    if not index:
        return "SparkX Skills 尚未安装，请直接按工具说明调用。"
    entry = next((item for item in index["skills"] if item["name"] == skill), None)
    if entry is None:
        return "没有这个 Skill。已安装：" + "、".join(item["name"] for item in index["skills"])
    file = (file or "SKILL.md").strip().lstrip("./")
    if file not in entry.get("files", []):
        return f"{skill} 中没有文件 {file[:120]}。可读文件：" + "、".join(entry.get("files", []))
    if type(offset) is not int or offset < 0:
        offset = 0
    try:
        text = (_root(settings) / index["folder"] / skill / file).read_text(encoding="utf-8")
    except OSError:
        return "Skill 文件暂时无法读取。"
    chunk = text[offset:offset + READ_CHUNK]
    header = (f"[{skill} {entry.get('version') or ''} · {file} · 第 {offset + 1}–{offset + len(chunk)} 字符，"
              f"共 {len(text)} 字符]\n")
    tail = (f"\n[未读完：继续读取请传 offset={offset + len(chunk)}]" if offset + len(chunk) < len(text) else "")
    return header + chunk + tail


if __name__ == "__main__":
    import sys

    from jx_alert.config import get_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if sys.argv[1:2] == ["install"]:
        result = install(get_settings(), ref=sys.argv[2] if len(sys.argv) > 2 else "main")
        print(json.dumps({"release": result["release"], "commit": result["commit"],
                          "skills": [(entry["name"], entry["version"]) for entry in result["skills"]]},
                         ensure_ascii=False))
    else:
        print(json.dumps(status(get_settings(), check=True), ensure_ascii=False, indent=1))
