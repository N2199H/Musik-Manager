"""Song-Ranking mit EWMA (Exponentially Weighted Moving Average).

Score-Skala: 0–100, Startwert 50 (neutral).
Pro Song wird genau ein Score gespeichert; bei jedem Play-Event wandert
der Score Richtung Target — träge (alpha=0.3), aber konsistent.

Targets:
    completed (>=90% gespielt)       → 75   (positiv)
    skipped_50_90 (50–90%)          → 65   (leicht positiv — "war ok, aber reichte")
    skipped_10_50 (10–50%)          → 35   (negativ)
    skipped_lt_10 (<10%)            → None (kein Update, Test/Falsches Lied)
    like                            → 80   (stark positiv)
    dislike                         → 25   (stark negativ)

Tradeoff: Frontend schickt nur Events mit, wenn das Frontend offen ist.
Background-Poller im Backend wäre overkill — der Sonos-Speaker weiß
selbst, was er spielt, und das Frontend pollt eh alle 5s.
"""

from typing import Optional

# Score-Gewichtung: 0.3 → träge, 0.5 → reaktiv. Bewusst konservativ.
ALPHA = 0.3

# Event-Targets (None = kein Score-Update)
TARGETS = {
    "completed": 75,
    "skipped_50_90": 65,
    "skipped_10_50": 35,
    "like": 80,
    "dislike": 25,
}


def compute_event(rel_pct: float) -> str:
    """Map einen gespielten Prozentsatz auf den passenden Event-Namen.

    Args:
        rel_pct: 0.0–1.0, Anteil des Songs der gespielt wurde (rel_time / duration).

    Returns:
        Einer von "completed", "skipped_50_90", "skipped_10_50", "skipped_lt_10".
    """
    if rel_pct >= 0.9:
        return "completed"
    if rel_pct >= 0.5:
        return "skipped_50_90"
    if rel_pct >= 0.10:
        return "skipped_10_50"
    return "skipped_lt_10"


def update_score(current: float, event: str, alpha: float = ALPHA) -> float:
    """Berechne den neuen Score via EWMA: neu = (1-alpha) * alt + alpha * target.

    Konvergiert asymptotisch gegen das Target, ohne es je zu erreichen —
    ein gutes Lied pendelt sich bei ~75 ein, ein schlechtes bei ~35.

    Args:
        current: aktueller Score (0–100).
        event: Event-Name (siehe TARGETS).
        alpha: Gewichtung (default 0.3).

    Returns:
        Neuer Score, geclampt auf [0, 100].
    """
    target = TARGETS.get(event)
    if target is None:
        return current  # "skipped_lt_10" → kein Update
    new = (1 - alpha) * current + alpha * target
    # Clamp — die Formel begrenzt sich selbst, aber Sicherheit schadet nicht
    return max(0.0, min(100.0, new))
