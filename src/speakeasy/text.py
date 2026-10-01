"""Fixes applied to every transcript, whichever engine made it."""
from __future__ import annotations

import re


def clean(cfg: dict, text: str) -> str:
    return remove_fillers(cfg, apply_replacements(cfg, text))


def apply_replacements(cfg: dict, text: str) -> str:
    for correct, variants in cfg.get("replacements", {}).items():
        for variant in variants:
            chunks = re.findall(r"[A-Za-z0-9]+", variant)
            if not chunks:
                continue
            sep = r"[\s\-./]*"
            pattern = r"(?<![A-Za-z0-9])" + sep.join(map(re.escape, chunks)) + r"(?![A-Za-z0-9])"
            text = re.sub(pattern, correct, text, flags=re.IGNORECASE)
    return text


def remove_fillers(cfg: dict, text: str) -> str:
    fillers = cfg.get("cleanup", {}).get("fillers", [])
    if not fillers:
        return text
    words = "|".join(map(re.escape, fillers))
    # The filler plus the punctuation and space after it ("Um, so" -> "so").
    text = re.sub(rf"(?<![\w'-])(?:{words})(?![\w'-])[,.!?]*\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+([,.!?])", r"\1", text)  # "so , then" left by a mid-sentence filler
    text = re.sub(r",([.!?])", r"\1", text)
    text = re.sub(r"^[\s,]+", "", text).rstrip()
    # Capitalize what is now the start of a sentence.
    return re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
