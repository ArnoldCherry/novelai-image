"""Persistent, owner-scoped storage for NovelAI workflow history and feedback."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import uuid


Owner = Tuple[Union[str, int], Union[str, int], Union[str, int]]
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|"
    r"authorization|auth[_-]?header|credential|cookie|client[_-]?secret|private[_-]?key)",
    re.IGNORECASE,
)
_MAX_JSON_BYTES = 1_000_000
_MAX_STYLE_VERSIONS = 30
_IMAGE_REFERENCE_KEY_RE = re.compile(
    r"^(?:image|mask)$|(?:(?:image|mask).*(?:path|ref|data|bytes|url|base64)|"
    r"(?:path|ref|data|bytes|url|base64).*(?:image|mask))",
    re.IGNORECASE,
)


class WorkflowStore:
    """Store workflow metadata under a hash of (platform, stream, user).

    Image data and references are retained only when ``retain_images`` is enabled.
    JSON writes use replace-on-success so readers never see partial records.
    """

    def __init__(
        self,
        root: Union[str, Path],
        ttl_hours: float = 24,
        retain_images: bool = False,
        max_records: int = 200,
    ) -> None:
        if ttl_hours <= 0:
            raise ValueError("ttl_hours must be greater than zero")
        if max_records <= 0:
            raise ValueError("max_records must be greater than zero")
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(hours=float(ttl_hours))
        self.retain_images = bool(retain_images)
        self.max_records = int(max_records)
        self._lock = threading.RLock()
        # Keep the original image handles in process memory for immediate workflow replay.
        self._memory_jobs: Dict[Tuple[str, str], Dict[str, Any]] = {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.isoformat(timespec="seconds")

    @staticmethod
    def _owner_hash(owner: Owner) -> str:
        if not isinstance(owner, (tuple, list)) or len(owner) != 3:
            raise ValueError("owner must be a (platform, stream, user) tuple")
        normalized = []
        for item in owner:
            if not isinstance(item, (str, int)):
                raise TypeError("owner values must be strings or integers")
            part = str(item).strip()
            if not part:
                raise ValueError("owner values cannot be empty")
            normalized.append(part)
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _namespace(self, owner: Owner) -> Path:
        return self.root / self._owner_hash(owner)

    def _remove_tree(self, target: Path) -> None:
        resolved = target.resolve()
        if resolved == self.root or not resolved.is_relative_to(self.root):
            raise ValueError("workflow cleanup target escapes data directory")
        shutil.rmtree(resolved)

    @staticmethod
    def _safe_id(value: str) -> str:
        if not isinstance(value, str) or not _JOB_ID_RE.fullmatch(value):
            raise ValueError("invalid workflow id")
        return value

    @classmethod
    def _sanitize(cls, value: Any) -> Any:
        """Drop credential-bearing object fields recursively before persistence."""
        if isinstance(value, Mapping):
            result: Dict[str, Any] = {}
            for key, child in value.items():
                key_text = str(key)
                if _SECRET_KEY_RE.search(key_text):
                    continue
                result[key_text] = cls._sanitize(child)
            return result
        if isinstance(value, (list, tuple)):
            return [cls._sanitize(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise TypeError(f"unsupported value for workflow metadata: {type(value).__name__}")

    @classmethod
    def _omit_image_references(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): cls._omit_image_references(child)
                for key, child in value.items()
                if not _IMAGE_REFERENCE_KEY_RE.search(str(key))
            }
        if isinstance(value, list):
            return [cls._omit_image_references(item) for item in value]
        return value

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return default

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def _expire_jobs(self, namespace: Path, now: Optional[datetime] = None) -> int:
        jobs_dir = namespace / "jobs"
        if not jobs_dir.is_dir():
            return 0
        cutoff = (now or self._now()) - self.ttl
        removed = 0
        expired_ids = set()
        for path in jobs_dir.glob("*.json"):
            try:
                record = self._read_json(path, {})
                created = datetime.fromisoformat(record["created_at"])
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                # Malformed records cannot be served and should not accumulate forever.
                created = datetime.min.replace(tzinfo=timezone.utc)
            if created < cutoff:
                record_id = path.stem
                path.unlink(missing_ok=True)
                expired_ids.add(record_id)
                self._memory_jobs.pop((namespace.name, record_id), None)
                image_dir = namespace / "images" / record_id
                if image_dir.exists():
                    self._remove_tree(image_dir)
                removed += 1
        # 明确偏好只保留画风摘要，独立于图片任务寿命；本人清除时一起删除。
        return removed

    def _load_job(self, owner: Owner, job_id: str) -> Optional[Dict[str, Any]]:
        job_id = self._safe_id(job_id)
        namespace = self._namespace(owner)
        self._expire_jobs(namespace)
        record = self._read_json(namespace / "jobs" / f"{job_id}.json", None)
        if record is None or record.get("owner_hash") != namespace.name:
            return None
        return record

    def record(
        self,
        owner: Owner,
        payload: Mapping[str, Any],
        images: Optional[Sequence[Any]],
        label: str,
        style: Optional[Mapping[str, Any]] = None,
        identity: Optional[Sequence[str]] = None,
    ) -> str:
        """Persist one generation record and return its opaque id."""
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        if not isinstance(label, str):
            raise TypeError("label must be a string")
        safe_payload = self._sanitize(payload)
        safe_style = self._sanitize(style) if style is not None else None
        disk_payload = safe_payload if self.retain_images else self._omit_image_references(safe_payload)
        metadata_limit = 80 * 1024 * 1024 if self.retain_images else _MAX_JSON_BYTES
        if len(json.dumps(disk_payload, ensure_ascii=False).encode("utf-8")) > metadata_limit:
            raise ValueError("workflow payload exceeds the 1 MB metadata limit")

        namespace = self._namespace(owner)
        now = self._now()
        job_id = uuid.uuid4().hex
        saved_images: List[Any] = []
        if self.retain_images and images:
            image_dir = namespace / "images" / job_id
            image_dir.mkdir(parents=True, exist_ok=True)
            for index, image in enumerate(images):
                if isinstance(image, Mapping):
                    saved_image = self._sanitize(image)
                    content = image.get("image")
                    if isinstance(content, bytes):
                        image_path = image_dir / f"{index:03d}.bin"
                        image_path.write_bytes(content)
                        saved_image["image"] = str(image_path)
                    elif isinstance(content, str):
                        decoded = None
                        try:
                            encoded = content.split(",", 1)[1] if content.startswith("data:image/") else content
                            if len(encoded) > 128:
                                decoded = base64.b64decode(encoded, validate=True)
                        except (binascii.Error, IndexError, ValueError):
                            decoded = None
                        if decoded:
                            image_path = image_dir / f"{index:03d}.bin"
                            image_path.write_bytes(decoded)
                            saved_image["image"] = {"$workflow_image_file": image_path.name}
                        else:
                            saved_image["image"] = content
                    elif isinstance(content, Path):
                        saved_image["image"] = str(content)
                    else:
                        raise TypeError("image mappings must have a bytes, path, or reference 'image'")
                    saved_images.append(saved_image)
                elif isinstance(image, bytes):
                    image_path = image_dir / f"{index:03d}.bin"
                    image_path.write_bytes(image)
                    saved_images.append(str(image_path))
                elif isinstance(image, (str, Path)):
                    saved_images.append(str(image))
                else:
                    raise TypeError("images must contain bytes, paths, or references")

        record = {
            "id": job_id,
            "owner_hash": namespace.name,
            "created_at": self._timestamp(now),
            "expires_at": self._timestamp(now + self.ttl),
            "payload": disk_payload,
            "images": saved_images,
            "images_persisted": self.retain_images,
            "requires_images_for_replay": self._omit_image_references(safe_payload) != safe_payload,
            "label": label[:300],
            "style": safe_style,
            "identity": self._sanitize(identity or []),
        }
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > metadata_limit:
            raise ValueError("workflow record exceeds the 1 MB metadata limit")
        with self._lock:
            jobs_dir = namespace / "jobs"
            jobs_dir.mkdir(parents=True, exist_ok=True)
            self._write_json(jobs_dir / f"{job_id}.json", record)
            self._memory_jobs[(namespace.name, job_id)] = {
                "payload": deepcopy(safe_payload),
                "images": deepcopy(list(images or [])),
            }
            # 图片默认只驻留内存，整个插件最多保留约256MiB；任务参数仍可在磁盘查询。
            def memory_size(value: Any) -> int:
                if isinstance(value, str):
                    return len(value)
                if isinstance(value, bytes):
                    return len(value)
                if isinstance(value, Mapping):
                    return sum(memory_size(child) for child in value.values())
                if isinstance(value, (list, tuple)):
                    return sum(memory_size(child) for child in value)
                return 0
            memory_bytes = sum(memory_size(value) for value in self._memory_jobs.values())
            while memory_bytes > 256 * 1024 * 1024 and self._memory_jobs:
                oldest = next(iter(self._memory_jobs))
                memory_bytes -= memory_size(self._memory_jobs.pop(oldest))
            self._expire_jobs(namespace, now)
            records = sorted(jobs_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
            for excess in records[self.max_records :]:
                old_id = excess.stem
                excess.unlink(missing_ok=True)
                self._memory_jobs.pop((namespace.name, old_id), None)
                old_images = namespace / "images" / old_id
                if old_images.exists():
                    self._remove_tree(old_images)
        return job_id

    def get(self, owner: Owner, job_id: str) -> Optional[Dict[str, Any]]:
        """Return a record only to its exact owner, or ``None`` if absent/expired."""
        namespace = self._namespace(owner)
        with self._lock:
            record = self._load_job(owner, job_id)
            if record is None:
                return None
            result = {key: record[key] for key in ("id", "payload", "images", "style", "label")}
            result["identity"] = record.get("identity", [])
            result["images_persisted"] = bool(record.get("images_persisted", False))
            if record.get("images_persisted"):
                result["images"] = self._restore_saved_images(namespace, job_id, result["images"])
            cached = self._memory_jobs.get((self._owner_hash(owner), job_id))
            result["replay_available"] = cached is not None or bool(record.get("images_persisted", False)) or not record.get("requires_images_for_replay", True)
            if cached is not None:
                result["payload"] = deepcopy(cached["payload"])
                result["images"] = deepcopy(cached["images"])
            return result

    @staticmethod
    def _restore_saved_images(namespace: Path, job_id: str, images: List[Any]) -> List[Any]:
        restored = []
        for image in images:
            if isinstance(image, Mapping):
                item = dict(image)
                reference = item.get("image")
                if isinstance(reference, Mapping) and set(reference) == {"$workflow_image_file"}:
                    filename = reference["$workflow_image_file"]
                    if not isinstance(filename, str) or not re.fullmatch(r"\d{3}\.bin", filename):
                        raise ValueError("invalid stored image reference")
                    item["image"] = base64.b64encode((namespace / "images" / job_id / filename).read_bytes()).decode("ascii")
                elif isinstance(reference, str):
                    path = Path(reference)
                    if path.is_absolute() and path.parent == namespace / "images" / job_id and path.suffix == ".bin":
                        item["image"] = base64.b64encode(path.read_bytes()).decode("ascii")
                restored.append(item)
            else:
                restored.append(image)
        return restored

    def list_jobs(self, owner: Owner, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        namespace = self._namespace(owner)
        with self._lock:
            self._expire_jobs(namespace)
            if limit is not None and int(limit) <= 0:
                return []
            paths = sorted(
                (namespace / "jobs").glob("*.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            ) if (namespace / "jobs").is_dir() else []
            rows = []
            for path in paths:
                record = self._read_json(path, {})
                if record.get("owner_hash") == namespace.name:
                    rows.append({"id": record["id"], "label": record.get("label", "")})
                    if limit is not None and len(rows) >= max(0, int(limit)):
                        break
            return rows

    def clear(self, owner: Owner) -> int:
        """Delete all jobs, image files, favorites, versions, and feedback for owner."""
        namespace = self._namespace(owner)
        with self._lock:
            if not namespace.exists():
                return 0
            jobs = list((namespace / "jobs").glob("*.json")) if (namespace / "jobs").is_dir() else []
            prefix = namespace.name
            self._memory_jobs = {key: value for key, value in self._memory_jobs.items() if key[0] != prefix}
            self._remove_tree(namespace)
            return len(jobs)

    def prune(self) -> int:
        """Remove expired jobs across all owner namespaces; safe for periodic calls."""
        removed = 0
        with self._lock:
            active_hashes = set()
            for namespace in self.root.iterdir():
                if not namespace.is_dir() or not re.fullmatch(r"[0-9a-f]{64}", namespace.name):
                    continue
                active_hashes.add(namespace.name)
                removed += self._expire_jobs(namespace)
            self._memory_jobs = {
                key: value for key, value in self._memory_jobs.items() if key[0] in active_hashes
            }
        return removed

    def save_favorite(
        self,
        owner: Owner,
        style_id: str,
        snapshot: Mapping[str, Any],
        label: str = "",
    ) -> Dict[str, Any]:
        """Save a favorite style snapshot as a new immutable version."""
        if not isinstance(style_id, str) or not style_id.strip():
            raise ValueError("style_id cannot be empty")
        if not isinstance(snapshot, Mapping):
            raise TypeError("snapshot must be a mapping")
        safe_snapshot = self._sanitize(snapshot)
        if len(json.dumps(safe_snapshot, ensure_ascii=False).encode("utf-8")) > _MAX_JSON_BYTES:
            raise ValueError("style snapshot exceeds the 1 MB metadata limit")
        namespace = self._namespace(owner)
        path = namespace / "favorites.json"
        with self._lock:
            favorites = self._read_json(path, {})
            item = favorites.get(style_id, {"style_id": style_id, "versions": [], "favorite": True})
            version = (item["versions"][-1]["version"] + 1) if item["versions"] else 1
            item["versions"].append({
                "version": version,
                "created_at": self._timestamp(self._now()),
                "snapshot": safe_snapshot,
            })
            item["versions"] = item["versions"][-_MAX_STYLE_VERSIONS:]
            item["current_version"] = version
            item["favorite"] = True
            if label:
                item["label"] = str(label)[:300]
            favorites[style_id] = item
            self._write_json(path, favorites)
            return {**item, "version": item["current_version"], "id": style_id}

    def list_favorites(self, owner: Owner) -> List[Dict[str, Any]]:
        favorites = self._read_json(self._namespace(owner) / "favorites.json", {})
        return [
            {"style_id": style_id, "id": style_id, "label": item.get("label", ""), "current_version": item["current_version"], "version": item["current_version"]}
            for style_id, item in favorites.items()
            if item.get("favorite") is True and item.get("versions")
        ]

    def get_favorite(self, owner: Owner, style_id: str) -> Optional[Dict[str, Any]]:
        favorites = self._read_json(self._namespace(owner) / "favorites.json", {})
        item = favorites.get(style_id)
        if item is None or item.get("favorite") is not True:
            return None
        result = dict(item)
        current = next((v for v in item["versions"] if v["version"] == item["current_version"]), None)
        result["snapshot"] = current["snapshot"] if current is not None else None
        result["id"] = style_id
        result["version"] = item["current_version"]
        result["history"] = item["versions"]
        return result

    def favorite(self, owner: Owner, name: str) -> Optional[Dict[str, Any]]:
        """Convenience lookup for a favorite's current style snapshot."""
        item = self.get_favorite(owner, name)
        return item.get("snapshot") if item is not None else None

    def rollback_favorite(self, owner: Owner, style_id: str, version: int) -> Dict[str, Any]:
        """Restore a prior snapshot by appending it as a new version."""
        current = self.get_favorite(owner, style_id)
        if current is None:
            raise KeyError(style_id)
        source = next((v for v in current["versions"] if v["version"] == version), None)
        if source is None:
            raise ValueError(f"favorite version {version} is unavailable")
        return self.save_favorite(owner, style_id, source["snapshot"], current.get("label", ""))

    def remove_favorite(self, owner: Owner, style_id: str) -> bool:
        """Unfavorite a style while keeping its version history for later recovery."""
        namespace = self._namespace(owner)
        path = namespace / "favorites.json"
        with self._lock:
            favorites = self._read_json(path, {})
            item = favorites.get(style_id)
            if item is None or item.get("favorite") is not True:
                return False
            item["favorite"] = False
            favorites[style_id] = item
            self._write_json(path, favorites)
            return True

    def record_feedback(
        self,
        owner: Owner,
        job_id: str,
        liked: Optional[bool] = None,
        rating: Optional[int] = None,
        recommended: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Record only the explicit feedback values supplied by the caller."""
        if liked is not None and not isinstance(liked, bool):
            raise TypeError("liked must be bool or None")
        if recommended is not None and not isinstance(recommended, bool):
            raise TypeError("recommended must be bool or None")
        if rating is not None and (isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5):
            raise ValueError("rating must be an integer from 1 to 5")
        if liked is None and rating is None and recommended is None:
            raise ValueError("at least one explicit feedback value is required")
        job = self.get(owner, job_id)
        if job is None:
            raise KeyError(job_id)
        values: Dict[str, Any] = {"job_id": job_id, "updated_at": self._timestamp(self._now())}
        if liked is not None:
            values["liked"] = liked
        if rating is not None:
            values["rating"] = rating
        if recommended is not None:
            values["recommended"] = recommended
        style = job.get("style")
        if isinstance(style, Mapping):
            values["style_hash"] = self._style_hash(style)
            style_id = style.get("style_id") or style.get("id")
            if not isinstance(style_id, str):
                favorites = self._read_json(self._namespace(owner) / "favorites.json", {})
                for candidate_id, favorite in favorites.items():
                    if favorite.get("favorite") is True and favorite.get("versions"):
                        latest = next((version.get("snapshot") for version in favorite["versions"] if version["version"] == favorite["current_version"]), None)
                        if latest == style:
                            style_id = candidate_id
                            break
            if isinstance(style_id, str):
                values["style_id"] = style_id
        namespace = self._namespace(owner)
        path = namespace / "feedback.json"
        with self._lock:
            feedback = self._read_json(path, {})
            feedback[job_id] = values
            feedback = dict(sorted(feedback.items(), key=lambda item: item[1].get("updated_at", ""), reverse=True)[:1000])
            self._write_json(path, feedback)
        return dict(values)

    def get_feedback(self, owner: Owner, job_id: str) -> Optional[Dict[str, Any]]:
        self._safe_id(job_id)
        feedback = self._read_json(self._namespace(owner) / "feedback.json", {})
        result = feedback.get(job_id)
        return dict(result) if result is not None else None

    @staticmethod
    def _style_hash(style: Mapping[str, Any]) -> str:
        return hashlib.sha256(json.dumps(dict(style), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def recommended(self, owner: Owner, limit: int = 10) -> List[Dict[str, Any]]:
        """按当前画风版本的明确正负反馈平滑计分，旧版本反馈不迁移。"""
        namespace = self._namespace(owner)
        self._expire_jobs(namespace)
        favorites = self._read_json(namespace / "favorites.json", {})
        feedback = self._read_json(namespace / "feedback.json", {})
        rows = []
        for style_id in favorites:
            row = self.get_favorite(owner, style_id)
            if row is None or not row.get("snapshot"):
                continue
            fingerprint = self._style_hash(row["snapshot"])
            total, count = 0, 0
            for values in feedback.values():
                if values.get("style_hash") != fingerprint:
                    continue
                score = (1 if values["liked"] else -1) if "liked" in values else 0
                score += (1 if values["recommended"] else -1) if "recommended" in values else 0
                score += int(values.get("rating", 3)) - 3
                total += score
                count += 1
            if total > 0:
                row["score"] = total / (count + 2)
                row["feedback_count"] = count
                rows.append(row)
        rows.sort(key=lambda row: (-row["score"], row.get("label", "").casefold(), row["style_id"]))
        return rows[:max(0, int(limit))]
