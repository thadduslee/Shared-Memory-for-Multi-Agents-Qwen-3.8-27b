"""Typed atomic-claim helpers used by the candidate memory component."""
from __future__ import annotations

import re

_PATTERNS = (
    ("phone_number", re.compile(
        r"(?<![\w+])(?:\+\d{1,3}[ .-]?(?:\(\d{2,4}\)[ .-]?)?\d{3,4}[ .-]\d{3,4}|"
        r"(?:\(\d{2,4}\)[ .-]?|\d{2,4}[ .-])\d{3,4}[ .-]\d{3,4}|"
        r"\d{3,4}[ .-]\d{3,4})(?!\w)")),
    ("date", re.compile(
        r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
        r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)"
        r"\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?\b|"
        r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", re.I)),
    ("time", re.compile(
        r"\b(?:[01]?\d|2[0-3]):[0-5]\d(?:\s*[ap]m)?\b|"
        r"\b(?:1[0-2]|0?[1-9])\s*[ap]m\b", re.I)),
    ("dose", re.compile(
        r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?\s*(?:mcg|mg|g|kg|ml|l|iu|units?)\b", re.I)),
    ("percentage", re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?\s*%")),
    ("identifier", re.compile(
        r"\b(?=[A-Z0-9_-]{4,}\b)(?=[A-Z0-9_-]*[A-Z])(?=[A-Z0-9_-]*\d)"
        r"[A-Z0-9]+(?:[-_][A-Z0-9]+)+\b")),
    ("quantity", re.compile(
        r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?\s*"
        r"(?:minutes?|hours?|days?|weeks?|months?|years?)\b", re.I)),
)
_STATUS = re.compile(
    r"\b(?:active|inactive|pending|confirmed|scheduled|rescheduled|cancelled|canceled|"
    r"completed|expired|revoked|deleted|superseded|denied|approved|accepted|rejected|"
    r"open|closed)\b", re.I)
_NEGATION = re.compile(
    r"\b(?:not|no|never|none|without|cannot|can't|don't|doesn't|didn't)\b", re.I)
_PERSON = re.compile(
    r"\b(?:Dr|Mr|Ms|Mrs|Prof)\.\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b|"
    r"\b(?:with|for|by|from|to)\s+([A-Z][a-z]+\s+[A-Z][a-z]+)\b")
_NUMBER = re.compile(r"(?<![\w:.,])[-+]?\d[\d,]*(?:\.\d+)?(?![\w:.,])")


def _role(text: str, start: int, kind: str) -> str:
    context = text[max(0, start - 48):start].lower()
    if kind == "time":
        if re.search(r"\b(?:arrive|arrival|check[ -]?in)\b", context):
            return "arrival_time"
        if re.search(r"\b(?:appointment|meeting|visit|session|starts?|scheduled)\b", context):
            return "appointment_time"
    return kind


def critical_values(text: str) -> list[dict[str, str]]:
    found: list[tuple[int, int, dict[str, str]]] = []
    occupied: list[tuple[int, int]] = []

    def add(kind: str, match: re.Match[str], surface: str | None = None) -> None:
        start, end = match.span(1) if match.lastindex else match.span()
        if any(start < old_end and end > old_start for old_start, old_end in occupied):
            return
        raw = surface if surface is not None else text[start:end]
        occupied.append((start, end))
        found.append((start, end, {
            "type": kind, "role": _role(text, start, kind),
            "canonical_value": " ".join(raw.lower().split()), "surface_value": raw,
        }))

    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            add(kind, match)
    for kind, pattern in (("status", _STATUS), ("negation", _NEGATION)):
        for match in pattern.finditer(text):
            add(kind, match)
    for match in _PERSON.finditer(text):
        add("person", match, match.group(1) or match.group(0))
    for match in _NUMBER.finditer(text):
        add("number", match)
    return [item for _, _, item in sorted(found, key=lambda row: row[:2])]
