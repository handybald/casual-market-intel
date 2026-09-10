"""Loads config/event_mapping.yaml and resolves provider-native event
names (MQL5, Forex Factory) to the canonical event_family key.

Mapping lives in config, not in parser code, so adding a new indicator
or fixing a provider label never requires touching parser logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .config import DEFAULT_EVENT_MAPPING_PATH


@dataclass(frozen=True)
class EventMappingEntry:
    event_family: str
    indicator: str
    release_bundle: Optional[str]
    mql5_names: List[str]
    forex_factory_names: List[str]


class EventMapping:
    def __init__(self, entries: Dict[str, EventMappingEntry]):
        self._entries = entries
        self._mql5_index: Dict[str, str] = {}
        self._ff_index: Dict[str, str] = {}
        for family, entry in entries.items():
            for name in entry.mql5_names:
                self._mql5_index[_normalize_name(name)] = family
            for name in entry.forex_factory_names:
                self._ff_index[_normalize_name(name)] = family

    def by_family(self, family: str) -> Optional[EventMappingEntry]:
        return self._entries.get(family)

    def resolve_mql5(self, raw_name: str) -> Optional[EventMappingEntry]:
        family = self._mql5_index.get(_normalize_name(raw_name))
        return self._entries.get(family) if family else None

    def resolve_forex_factory(self, raw_name: str) -> Optional[EventMappingEntry]:
        family = self._ff_index.get(_normalize_name(raw_name))
        return self._entries.get(family) if family else None

    def families(self) -> List[str]:
        return list(self._entries.keys())

    def families_in_bundle(self, bundle: str) -> List[str]:
        return [f for f, e in self._entries.items() if e.release_bundle == bundle]


def _normalize_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def load_event_mapping(path: Optional[Path] = None) -> EventMapping:
    p = path or DEFAULT_EVENT_MAPPING_PATH
    with open(p, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    entries: Dict[str, EventMappingEntry] = {}
    for family, body in raw.items():
        entries[family] = EventMappingEntry(
            event_family=family,
            indicator=body["indicator"],
            release_bundle=body.get("release_bundle"),
            mql5_names=list(body.get("mql5", [])),
            forex_factory_names=list(body.get("forex_factory", [])),
        )
    return EventMapping(entries)
