import json
from pathlib import Path


class Seen:
    LIMIT = 500

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries = []
        self.dirty = False
        if path.exists():
            entries = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(entries, list) or any(
                not isinstance(entry, dict)
                or type(entry.get("alerted")) is not bool
                or not isinstance(entry.get("keys"), list)
                or not entry["keys"]
                or any(not isinstance(key, str) for key in entry["keys"])
                for entry in entries
            ):
                raise ValueError("Invalid seen.json")
            self.entries = entries[-self.LIMIT:]
            self.dirty = len(entries) > self.LIMIT

    def contains(self, keys: tuple[str, ...], *, alerted_only: bool = False) -> bool:
        return any(
            (not alerted_only or entry["alerted"]) and any(key in entry["keys"] for key in keys)
            for entry in self.entries
        )

    def remember(self, keys: tuple[str, ...], *, alerted: bool = False) -> None:
        self.entries.append({"keys": list(dict.fromkeys(keys)), "alerted": alerted})
        self.entries = self.entries[-self.LIMIT:]
        self.dirty = True

    def save(self) -> None:
        if self.dirty:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)
            self.dirty = False
