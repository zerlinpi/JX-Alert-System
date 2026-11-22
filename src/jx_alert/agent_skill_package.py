"""Inert, bounded interchange. Archive names are data, never filesystem paths."""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import posixpath
import re
import stat
import struct
import unicodedata
import zipfile
import zlib

from jx_alert.agent_skill_document import (
    LibraryError, MAX_BYTES, SkillDocument, _unsafe_text, decode_file, parse_document, valid_name,
)

MAX_FILES = 32
MAX_ENTRIES = 64  # includes optional directory records
MAX_TOTAL = 512 * 1024
MAX_ARCHIVE = 576 * 1024
MAX_REQUEST = 4 * ((MAX_ARCHIVE + 2) // 3) + 1024
_MAX_CENTRAL = 32 * 1024
_ALLOWED_COMPRESSION = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
_RESERVED = re.compile(r"(?:con|aux|prn|nul|(?:com|lpt)[1-9¹²³])", re.I)
_BAD_PATH = "文件名或目录不兼容。只保留 SKILL.md 和 references/ 中的 .md、.txt；不支持隐藏文件、脚本、链接或其他资源。"


@dataclass(frozen=True)
class SkillPackage:
    document: SkillDocument
    references: dict[str, str]  # relative path -> original UTF-8 text
    digest: str


def _package_digest(document, references):
    if not references:
        return document.digest  # identical identity to pre-package single-file revisions
    h = sha256(b"jx-skill-package-v1\0")
    for path, source in sorted({"SKILL.md": document.source, **references}.items()):
        for item in (path.encode(), source.encode()):
            h.update(len(item).to_bytes(4, "big"))
            h.update(item)
    return h.hexdigest()


def _reserved_name(component):
    return bool(_RESERVED.fullmatch(component.partition(".")[0].rstrip(" ")))


def _validate_path(path, *, outer=False):
    parts = path.split("/")
    if (not path or len(path.encode()) > (305 if outer else 240) or len(parts) > (9 if outer else 8)
            or any(not p or p != unicodedata.normalize("NFC", p) or p.startswith((".", " "))
                   or p.endswith((".", " ")) or len(p.encode()) > 100 or _reserved_name(p)
                   or any(not (c.isalnum() or c in " -_.") for c in p) for p in parts)):
        raise LibraryError(_BAD_PATH)


def _inspect_entries(z):
    infos = z.infolist()
    if not 1 <= len(infos) <= MAX_ENTRIES:
        raise LibraryError("技能包条目过多。", 413)
    rows, seen, total = [], set(), 0
    for entry in infos:
        path = entry.filename.removesuffix("/")
        _validate_path(path, outer=True)
        if entry.filename != entry.orig_filename or path.casefold() in seen:
            raise LibraryError("文件名被截断、重复或大小写冲突。")
        seen.add(path.casefold())
        kind = stat.S_IFMT(entry.external_attr >> 16)
        # A directory is identified by its slash, not by implicit archive metadata.
        if (kind not in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG)
                or entry.external_attr & 0x400  # Windows reparse point
                or (entry.external_attr & 0x10 and not entry.is_dir())):
            raise LibraryError("技能包不接受符号链接或其他特殊条目。")
        if entry.volume or entry.compress_type not in _ALLOWED_COMPRESSION or entry.flag_bits & ~0x80e:
            raise LibraryError("请使用未加密的普通 ZIP（存储或 Deflate）。")
        if entry.file_size > MAX_BYTES or (entry.is_dir() and entry.file_size):
            raise LibraryError("技能包中单个文档最大为 128 KiB。", 413)
        total += entry.file_size
        rows.append((path, entry))
    files = [p for p, entry in rows if not entry.is_dir()]
    if len(files) > MAX_FILES or total > MAX_TOTAL:
        raise LibraryError("技能包最多 32 个文件，解压后总计不超过 512 KiB。", 413)
    roots = (["SKILL.md"] if "SKILL.md" in files else
             [p for p in files if p.endswith("/SKILL.md") and p.count("/") == 1])
    if len(roots) != 1:
        raise LibraryError("每个 ZIP 只可包含一个技能；请单独打包 SKILL.md 及其 references/。")
    wrapper = roots[0].rpartition("/")[0]
    if wrapper and not valid_name(wrapper):
        raise LibraryError("外层目录必须与技能 name 相同，或直接打包 SKILL.md 和 references/。")
    members, child_dirs = {}, {}
    for path, entry in rows:
        if wrapper and path == wrapper and entry.is_dir():
            continue
        if wrapper and not path.startswith(wrapper + "/"):
            raise LibraryError(_BAD_PATH)
        rel = path[len(wrapper) + 1:] if wrapper else path
        _validate_path(rel)
        if entry.is_dir():
            if rel != "references" and not rel.startswith("references/"):
                raise LibraryError(_BAD_PATH)
        elif rel != "SKILL.md" and not (rel.startswith("references/") and rel.lower().endswith((".md", ".txt"))):
            raise LibraryError(_BAD_PATH)
        else:
            members[rel] = entry
        # Reject implicit directory/file collisions and directory spelling aliases too.
        p = rel.split("/")
        for n in range(1, len(p) + int(entry.is_dir())):
            prefix = "/".join(p[:n])
            if child_dirs.setdefault(prefix.casefold(), prefix) != prefix:
                raise LibraryError("目录大小写冲突。")
    if any(p.casefold() in child_dirs for p in members):
        raise LibraryError("文件与目录路径冲突。")
    return members, wrapper


_LOCAL_HEADER = struct.Struct("<4s5H3I2H")
_DESCRIPTOR = struct.Struct("<3I")


def _exact_member(raw, info, end):
    """Read only one bounded local member, with its full size, CRC and framing."""
    start = info.header_offset
    if start < 0 or start + _LOCAL_HEADER.size > end:
        raise LibraryError("ZIP 本地文件头有重叠或缺失。")
    signature, _, flags, method, _, _, crc, csize, size, name_size, extra_size = _LOCAL_HEADER.unpack_from(raw, start)
    first_name = start + _LOCAL_HEADER.size
    first_data = first_name + name_size + extra_size
    after_data = first_data + info.compress_size
    name = info.orig_filename.encode("utf-8" if flags & 0x800 else "cp437")
    if (signature != b"PK\x03\x04" or flags != info.flag_bits or method != info.compress_type
            or name != raw[first_name:first_name+name_size] or after_data > end):
        raise LibraryError("ZIP 本地文件头与中央目录不符。")
    declared = (info.CRC, info.compress_size, info.file_size)
    if flags & 8:
        # Standard streamed ZIPs put three 32-bit fields behind the compressed
        # data, with an optional signature. ZIP64 descriptors are not supported.
        if any(a not in (0, b) for a, b in zip((crc, csize, size), declared, strict=True)):
            raise LibraryError("ZIP 本地大小或校验值不符。")
        descriptor = raw[after_data:end]
        if len(descriptor) == 16 and descriptor[:4] == b"PK\x07\x08":
            descriptor = descriptor[4:]
        if len(descriptor) != 12 or _DESCRIPTOR.unpack(descriptor) != declared:
            raise LibraryError("ZIP 流式目录的大小或校验值不符。")
    elif (crc, csize, size) != declared or after_data != end:
        raise LibraryError("ZIP 本地大小或校验值不符。")
    data = raw[first_data:after_data]
    if method == zipfile.ZIP_DEFLATED:
        # ZipExtFile clips to header size; use zlib directly to verify actual EOF
        # without ever allocating more than the prevalidated member size plus 1.
        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        data = decoder.decompress(data, info.file_size + 1)
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise LibraryError("ZIP 压缩文档超出声明大小、存在尾部数据或未完整结束。")
    if len(data) != info.file_size or zlib.crc32(data) != info.CRC:
        raise LibraryError("ZIP 文档大小或校验值不符。")
    return data


def parse_package(encoded):
    raw = decode_file(encoded, MAX_ARCHIVE)
    # Bound the central-directory parser before it allocates ZipInfo objects. This
    # subset excludes archive comments, ZIP64, split archives and self-extractors.
    if len(raw) < 22 or raw[-22:-18] != b"PK\x05\x06" or not raw.startswith(b"PK\x03\x04"):
        raise LibraryError("请选择不带注释的普通 ZIP 技能包。")
    disk, central_disk, n, count, size, start, comment = struct.unpack_from("<4H2IH", raw, len(raw)-18)
    if (disk or central_disk or comment or not 0 < count == n <= MAX_ENTRIES
            or size > _MAX_CENTRAL or start + size != len(raw) - 22):
        raise LibraryError("不支持分卷、超限或非常规 ZIP。")
    try:
        with zipfile.ZipFile(BytesIO(raw)) as z:
            if len(z.infolist()) != count or z.start_dir != start:
                raise LibraryError("ZIP 目录不一致。")
            members, wrapper = _inspect_entries(z)
            # Require contiguous, non-overlapping local members, including empty
            # directories. Never interpret file names as filesystem paths.
            ordered = sorted(z.infolist(), key=lambda i: i.header_offset)
            if ordered[0].header_offset != 0:
                raise LibraryError("ZIP 本地目录起始位置不符。")
            ends = [i.header_offset for i in ordered[1:]] + [start]
            paths = {id(info): path for path, info in members.items()}
            files = {}
            for info, end in zip(ordered, ends, strict=True):
                data = _exact_member(raw, info, end)
                if id(info) in paths:
                    files[paths[id(info)]] = data
            document = parse_document(files.pop("SKILL.md"))
            if wrapper and wrapper != document.name:
                raise LibraryError("ZIP 外层目录必须与 SKILL.md 的 name 相同。")
            refs = {path: data.decode("utf-8") for path, data in files.items()}
            if any(_unsafe_text(s) for s in refs.values()):
                raise LibraryError("参考文档必须为 UTF-8 纯文本。")
    except (zipfile.BadZipFile, UnicodeError, RuntimeError, OSError, EOFError, zlib.error, struct.error):
        raise LibraryError("无法读取 ZIP，请核对编码、压缩方法及文件完整性。") from None
    return SkillPackage(document, refs, _package_digest(document, refs))


def export_zip(name, source, references):
    """Deterministic stored ZIP, strips all incoming archive metadata."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", allowZip64=False) as z:
        for path, text in sorted({"SKILL.md": source, **references}.items()):
            # Windows device names can occur in legacy single-file skill names.
            filename = path if _reserved_name(name) else f"{name}/{path}"
            info = zipfile.ZipInfo(filename, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            z.writestr(info, text.encode("utf-8"))
    return buf.getvalue()


# Advisory only: simple Markdown inline links to local text. Never resolve URLs,
# inspect the filesystem, or treat a complete list as permission to use any tool.
_TEXT_LINK = re.compile(r"\]\(([^()<>\s?#:]{1,240}\.(?:md|txt))(?:#[^()\s]{1,120})?\)", re.I)


def missing_text_links(source, references):
    files = {"SKILL.md": source, **references}
    missing = []
    for path, text in files.items():
        for match in _TEXT_LINK.finditer(text):
            target = match[1]
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            if resolved not in files and (path, target) not in missing:
                missing.append((path, target))
                if len(missing) == 20:
                    return missing
    return missing
