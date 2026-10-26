from dataclasses import dataclass
from copy import copy
from pathlib import Path, PurePosixPath, PureWindowsPath
import os
from tempfile import NamedTemporaryFile
from threading import RLock

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.adapters.common import AdapterError


class SkillMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
    description: str = Field(min_length=1, max_length=1024)


class SkillResource(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    path: str
    type: str


class SkillSummary(SkillMetadata):
    resources: tuple[SkillResource, ...]


class SkillStatus(SkillSummary):
    is_enabled: bool


class SkillState(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    disabled_skills: list[str]


@dataclass(frozen=True)
class SkillEntry:
    directory: Path
    root: Path
    summary: SkillSummary


def skill_error(code: str, message: str) -> AdapterError:
    return AdapterError(code, False, message, None)


def read_frontmatter(stream) -> SkillMetadata:
    if stream.readline().decode('utf-8-sig').strip() != '---':
        raise skill_error('invalid_skill_metadata', 'SKILL.md 缺少 YAML frontmatter')
    lines = []
    for raw in stream:
        line = raw.decode('utf-8')
        if line.strip() == '---':
            break
        lines.append(line)
    else:
        raise skill_error('invalid_skill_metadata', 'SKILL.md frontmatter 未闭合')
    source = ''.join(lines)
    try:
        node = yaml.compose(source, Loader=yaml.SafeLoader)
        if not isinstance(node, yaml.MappingNode):
            raise ValueError('元数据必须为映射')
        if any(not isinstance(key, yaml.ScalarNode) or key.tag != 'tag:yaml.org,2002:str' for key, _ in node.value):
            raise ValueError('元数据字段必须为字符串')
        keys = [key.value for key, _ in node.value]
        if len(keys) != len(set(keys)) or set(keys) != {'name', 'description'}:
            raise ValueError('元数据字段重复')
        return SkillMetadata.model_validate(yaml.safe_load(source))
    except (yaml.YAMLError, ValidationError, ValueError):
        raise skill_error('invalid_skill_metadata', 'SKILL.md 元数据无效：仅允许唯一的 name/description 字符串字段') from None


def resource_file(directory: Path, root: Path, relative: str) -> Path:
    parts = relative.split('/')
    if (not relative or any(part in {'', '.', '..'} for part in parts)
            or '\\' in relative or '\x00' in relative or ':' in relative
            or PurePosixPath(relative).is_absolute() or PureWindowsPath(relative).is_absolute()):
        raise skill_error('unsafe_skill_path', '技能资源必须使用无越界的规范相对路径')
    try:
        if directory.resolve(strict=True) != root:
            raise skill_error('unsafe_skill_path', '已登记技能目录发生变化')
        target = (root / relative).resolve(strict=True)
        if not target.is_relative_to(root):
            raise skill_error('unsafe_skill_path', '技能资源超出已登记目录')
        if not target.is_file():
            raise skill_error('unknown_skill_resource', '技能资源不是文本文件')
        return target
    except (OSError, RuntimeError):
        raise skill_error('unknown_skill_resource', '技能资源不存在或不可读') from None


def discover_skill(directory: Path) -> SkillEntry:
    directory = directory.absolute()
    try:
        root = directory.resolve(strict=True)
        with resource_file(directory, root, 'SKILL.md').open('rb') as stream:
            metadata = read_frontmatter(stream)
        resources = []
        for path in sorted(root.rglob('*')):
            if path.name == 'SKILL.md' or path.suffix.lower() not in {'.md', '.txt', '.py'}:
                continue
            relative = path.relative_to(root).as_posix()
            resource_file(directory, root, relative)
            resources.append(SkillResource(path=relative, type='script' if path.suffix.lower() == '.py' else 'reference'))
        return SkillEntry(directory, root, SkillSummary(**metadata.model_dump(), resources=tuple(resources)))
    except (OSError, UnicodeError, RuntimeError):
        raise skill_error('invalid_skill_metadata', '已登记技能目录或 SKILL.md 不可读') from None


def discover_entries(directories: tuple[Path, ...]) -> dict[str, SkillEntry]:
    entries = {}
    for directory in directories:
        try:
            root = directory.resolve(strict=True)
            if not root.is_dir():
                raise skill_error('invalid_skill_directory', '已登记技能路径不是目录')
            candidates = [directory] if (root / 'SKILL.md').exists() or (root / 'SKILL.md').is_symlink() else []
            if not candidates:
                for child in sorted(root.iterdir()):
                    if child.is_symlink() and not child.resolve(strict=True).is_relative_to(root):
                        raise skill_error('unsafe_skill_path', '技能子目录超出已登记目录')
                    if child.is_dir() and ((child / 'SKILL.md').exists() or (child / 'SKILL.md').is_symlink()):
                        candidates.append(directory / child.name)
        except (OSError, RuntimeError):
            raise skill_error('invalid_skill_directory', '已登记技能目录不存在或不可读') from None
        for candidate in candidates:
            entry = discover_skill(candidate)
            name = entry.summary.name
            if name in entries:
                raise skill_error('duplicate_skill_name', f'技能名称重复：{name}')
            entries[name] = entry
    return entries


class SkillCatalog:
    def __init__(self, directories: tuple[Path, ...], state_path: Path | None = None):
        self.directories = tuple(directory.absolute() for directory in directories)
        self.entries = discover_entries(self.directories)
        self.state_path = state_path
        self.lock = RLock()
        self.disabled_skills = frozenset()
        if state_path is not None:
            try:
                self.disabled_skills = frozenset(SkillState.model_validate_json(state_path.read_bytes()).disabled_skills)
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                raise skill_error('invalid_skill_state', '技能启停状态文件不可读或格式无效') from None

    def summaries(self, enabled_names: frozenset[str] | None = None) -> list[SkillSummary]:
        with self.lock:
            return [self.entries[name].summary for name in sorted(self.entries)
                    if enabled_names is None or name in enabled_names]

    def introductions(self, enabled_names: frozenset[str] | None = None) -> list[dict[str, str]]:
        return [{'name': item.name, 'description': item.description} for item in self.summaries(enabled_names)]

    def enabled_names(self) -> frozenset[str]:
        with self.lock:
            return frozenset(self.entries) - self.disabled_skills

    def statuses(self) -> list[SkillStatus]:
        with self.lock:
            enabled = self.enabled_names()
            return [SkillStatus(**item.model_dump(), is_enabled=item.name in enabled) for item in self.summaries()]

    def snapshot(self):
        with self.lock:
            snapshot = copy(self)
            snapshot.entries = dict(self.entries)
            snapshot.lock = RLock()
            snapshot.state_path = None
            return snapshot

    def refresh(self) -> list[SkillStatus]:
        with self.lock:
            entries = discover_entries(self.directories)
            self.entries = entries
            return self.statuses()

    def set_enabled(self, name: str, enabled: bool) -> SkillStatus:
        with self.lock:
            entry = self.entry(name)
            disabled = set(self.disabled_skills)
            disabled.discard(name) if enabled else disabled.add(name)
            temporary = None
            try:
                if self.state_path is not None:
                    self.state_path.parent.mkdir(parents=True, exist_ok=True)
                    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.state_path.parent,
                                            suffix='.tmp', delete=False) as stream:
                        temporary = Path(stream.name)
                        stream.write(SkillState(disabled_skills=sorted(disabled)).model_dump_json() + '\n')
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.state_path)
                self.disabled_skills = frozenset(disabled)
            except OSError:
                raise skill_error('skill_state_save_failed', '技能启停状态保存失败，原状态已保留') from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return SkillStatus(**entry.summary.model_dump(), is_enabled=enabled)

    def entry(self, name: str) -> SkillEntry:
        with self.lock:
            if name not in self.entries:
                raise skill_error('unknown_skill', '技能未登记')
            return self.entries[name]

    def load(self, name: str) -> dict:
        entry = self.entry(name)
        path = resource_file(entry.directory, entry.root, 'SKILL.md')
        try:
            with path.open('rb') as stream:
                metadata = read_frontmatter(stream)
                if metadata.model_dump() != entry.summary.model_dump(exclude={'resources'}):
                    raise skill_error('skill_metadata_changed', '技能元数据已变化，请重新初始化目录')
                content = stream.read().decode('utf-8')
                if '\x00' in content:
                    raise UnicodeError()
            return {**entry.summary.model_dump(mode='json'), 'content': content}
        except (OSError, UnicodeError):
            raise skill_error('unreadable_skill_resource', '技能正文不是可读 UTF-8 文本') from None

    def read(self, name: str, relative: str) -> dict:
        entry = self.entry(name)
        path = resource_file(entry.directory, entry.root, relative)
        if relative not in {item.path for item in entry.summary.resources}:
            raise skill_error('unknown_skill_resource', '技能资源未登记')
        try:
            content = path.read_text(encoding='utf-8-sig')
            if '\x00' in content:
                raise UnicodeError()
            return {'name': name, 'path': relative, 'content': content}
        except (OSError, UnicodeError):
            raise skill_error('unreadable_skill_resource', '技能资源不是可读 UTF-8 文本') from None
