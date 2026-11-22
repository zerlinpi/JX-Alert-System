"""Validate a small, inert, single-file subset of the Agent Skills interchange format."""

import base64
import binascii
from dataclasses import dataclass
from hashlib import sha256
import re
import unicodedata

import yaml
from yaml.nodes import MappingNode, ScalarNode


MAX_BYTES = 16 * 1024
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)
_OBJECTIVES = frozenset({"balanced", "reach", "efficiency", "discovery"})
_STRING = "tag:yaml.org,2002:str"
_FIELDS = frozenset({"name", "description", "metadata", "license", "compatibility"})


class LibraryError(ValueError):
    def __init__(self, detail, status_code=400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class SkillDocument:
    name: str
    description: str
    objective: str | None
    digest: str
    source: str


def valid_name(value):
    return isinstance(value, str) and 1 <= len(value) <= 64 and bool(NAME.fullmatch(value))


def _mapping(node):
    if not isinstance(node, MappingNode) or len(node.value) > 32:
        raise ValueError
    result = {}
    for key, value in node.value:
        if (not isinstance(key, ScalarNode) or key.tag != _STRING
                or not 1 <= len(key.value) <= 64 or key.value in result):
            raise ValueError
        result[key.value] = value
    return result


def _string(node, limit):
    if (not isinstance(node, ScalarNode) or node.tag != _STRING or not node.value.strip()
            or len(node.value) > limit or _unsafe_text(node.value)):
        raise ValueError
    return node.value


def _unsafe_text(text):
    # Reject binary text, NUL, directional overrides and invisible format controls.
    return any(c not in "\t\r\n" and unicodedata.category(c) in {"Cc", "Cs", "Cf"} for c in text)


def parse_upload(filename, encoded):
    if filename != "SKILL.md" or not isinstance(encoded, str):
        raise LibraryError("只接受单个名为 SKILL.md 的 UTF-8 文本文件；不支持压缩包、脚本或链接。")
    if len(encoded) > ((MAX_BYTES + 2) // 3) * 4:
        raise LibraryError("SKILL.md 最大为 16 KiB。", 413)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise LibraryError("文件编码无效。") from None
    if len(raw) > MAX_BYTES:
        raise LibraryError("SKILL.md 最大为 16 KiB。", 413)
    try:
        source = raw.decode("utf-8")
        if _unsafe_text(source):
            raise ValueError
        lines = source.replace("\r\n", "\n").split("\n")
        if lines[0] != "---":
            raise ValueError
        end = lines.index("---", 1)
        if end > 100 or not "\n".join(lines[end + 1:]).strip():
            raise ValueError
        header = "\n".join(lines[1:end])
        if len(header.encode("utf-8")) > 4096:
            raise ValueError
        # Compose nodes only, never construct Python objects. Reject references and
        # excessive nesting *before* composing the bounded, shallow front matter.
        depth = 0
        for i, event in enumerate(yaml.parse(header, Loader=yaml.SafeLoader)):
            if (i > 160 or getattr(event, "anchor", None) is not None
                    or getattr(event, "tag", None) is not None):
                raise ValueError
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > 2 or isinstance(event, yaml.SequenceStartEvent):
                    raise ValueError
            elif isinstance(event, yaml.MappingEndEvent):
                depth -= 1
        value = _mapping(yaml.compose(header, Loader=yaml.SafeLoader))
        if "allowed-tools" in value:
            raise LibraryError("此资料库不接受 allowed-tools 工具权限声明。")
        if value.keys() - _FIELDS:
            raise ValueError
        name = _string(value["name"], 64)
        if not valid_name(name):
            raise ValueError
        description = _string(value["description"], 1024)
        for key, limit in (("license", 512), ("compatibility", 500)):
            if key in value:
                _string(value[key], limit)
        metadata = ({key: _string(node, 512) for key, node in _mapping(value["metadata"]).items()}
                    if "metadata" in value else {})
        objective = metadata.get("jx-review-objective")
        if objective is not None and objective not in _OBJECTIVES:
            raise LibraryError("jx-review-objective 仅接受 balanced、efficiency、reach、discovery。")
    except LibraryError:
        raise
    except (UnicodeError, ValueError, IndexError, KeyError, yaml.YAMLError, RecursionError):
        raise LibraryError("技能格式无效。需要 YAML 的 name、description 和非空 Markdown 正文。") from None
    return SkillDocument(name, description, objective, sha256(raw).hexdigest(), source)
